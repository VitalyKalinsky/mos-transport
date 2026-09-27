"""Клиент дорожного роутера OSRM: привязка треков к дорогам, маршруты, объезды.

По умолчанию используется публичный демо-сервер OSRM (~1 запрос/с). Для продакшена нужен свой OSRM
на выгрузке OSM «Центральный ФО», адрес задаётся ``ROUTER_URL``. Вызовы синхронные (urllib без
зависимостей), из asyncio их нужно вызывать через ``asyncio.to_thread``.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

ROUTER_URL = os.getenv("ROUTER_URL", "https://router.project-osrm.org")
USER_AGENT = "mos-transport-dispatcher/1.0"  # правила публичных серверов OSM: клиент должен представляться
_MIN_INTERVAL = float(os.getenv("ROUTER_MIN_INTERVAL_S", "1.05"))   # лимит публичного сервера 1 rps
_lock = threading.Lock()   # общий лимит для всех потоков to_thread
_last = [0.0]


class RoutingError(RuntimeError):
    """Роутер недоступен, вернул HTTP-ошибку или ``code != "Ok"`` (нет маршрута)."""


def _get(path: str, params: dict, timeout: float = 20.0) -> dict:
    """GET к OSRM с глобальным ограничением частоты.

    Args:
        path: Путь сервиса OSRM с координатами.
        params: Query-параметры.
        timeout: Таймаут запроса, с.

    Returns:
        dict: JSON-ответ OSRM с ``code == "Ok"``.

    Raises:
        RoutingError: Сетевая ошибка, HTTP-ошибка или ответ OSRM с ошибкой.
    """
    with _lock:
        wait = _MIN_INTERVAL - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    url = f"{ROUTER_URL}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # OSRM на 400 отдаёт JSON с кодом (NoRoute, NoMatch) — он информативнее статуса
        try:
            data = json.loads(e.read())
        except Exception:  # noqa: BLE001
            raise RoutingError(f"HTTP {e.code}") from e
    except Exception as e:  # noqa: BLE001
        raise RoutingError(str(e)) from e
    if data.get("code") != "Ok":
        raise RoutingError(data.get("code", "error") + ": " + str(data.get("message", "")))
    return data


def _coords(points: list[list[float]]) -> str:
    return ";".join(f"{p[1]:.6f},{p[0]:.6f}" for p in points)     # OSRM ждёт lon,lat, у нас [lat, lon]


def route(points: list[list[float]], exclude: list[list[float]] | None = None) -> dict:
    """Маршрут по дорогам через заданные точки.

    Args:
        points: Точки ``[[lat, lon], ...]`` в порядке следования (минимум 2).
        exclude: Не используется (OSRM не умеет исключать участки; объезд строится через точку в стороне).

    Returns:
        dict: ``coords`` (``[[lat, lon], ...]``), ``distance_m``, ``duration_s``.

    Raises:
        RoutingError: Роутер недоступен или маршрут не найден.
    """
    # continue_straight — без разворотов на промежуточных точках (автобус не разворачивается у остановки)
    data = _get(f"/route/v1/driving/{_coords(points)}",
                {"overview": "full", "geometries": "geojson", "continue_straight": "true"})
    r = data["routes"][0]
    return {"coords": [[c[1], c[0]] for c in r["geometry"]["coordinates"]],
            "distance_m": r["distance"], "duration_s": r["duration"]}


def match(points: list[list[float]], timestamps: list[int] | None = None,
          radiuses: list[float] | None = None) -> dict:
    """Map matching: привязка GPS-трека к дорожному графу.

    Args:
        points: Точки трека ``[[lat, lon], ...]``.
        timestamps: Unix-время точек; помогает OSRM отсечь нереальные скорости.
        radiuses: Радиус поиска дороги для каждой точки, м (≈ точность GPS).

    Returns:
        dict: ``coords``, ``distance_m`` и ``confidence`` (минимум по фрагментам, 0..1).

    Raises:
        RoutingError: Роутер недоступен или трек не привязался.
    """
    # tidy — прореживание плотных точек; gaps=ignore — не рвать трек на паузах в 10–15 с между пакетами NDTP
    params = {"overview": "full", "geometries": "geojson", "tidy": "true", "gaps": "ignore"}
    if timestamps:
        params["timestamps"] = ";".join(str(int(t)) for t in timestamps)
    if radiuses:
        params["radiuses"] = ";".join(f"{r:.0f}" for r in radiuses)
    data = _get(f"/match/v1/driving/{_coords(points)}", params)
    coords: list[list[float]] = []
    dist, conf = 0.0, []
    for m in data["matchings"]:
        for c in m["geometry"]["coordinates"]:
            coords.append([c[1], c[0]])
        dist += m["distance"]
        conf.append(m.get("confidence", 0))
    # min, а не среднее: один плохой фрагмент портит геометрию всего перегона
    return {"coords": coords, "distance_m": dist, "confidence": min(conf) if conf else 0.0}
