"""Клиент дорожного роутера OSRM (привязка треков к дорогам, построение маршрутов и объездов).

По умолчанию — публичный демо-сервер OSRM (ограничение ~1 запрос/с). Для продакшена:
свой OSRM на выгрузке OSM «Центральный ФО» (см. README, раздел «Геометрия маршрутов»),
адрес задаётся ROUTER_URL. Все вызовы синхронные (вызывать через asyncio.to_thread).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

ROUTER_URL = os.getenv("ROUTER_URL", "https://router.project-osrm.org")
USER_AGENT = "mos-transport-dispatcher/1.0"
_MIN_INTERVAL = float(os.getenv("ROUTER_MIN_INTERVAL_S", "1.05"))   # вежливость к публичному серверу
_lock = threading.Lock()
_last = [0.0]


class RoutingError(RuntimeError):
    pass


def _get(path: str, params: dict, timeout: float = 20.0) -> dict:
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
    return ";".join(f"{p[1]:.6f},{p[0]:.6f}" for p in points)     # OSRM: lon,lat


def route(points: list[list[float]], exclude: list[list[float]] | None = None) -> dict:
    """Маршрут по дорогам через точки [[lat, lon], ...]. → {coords, distance_m, duration_s}."""
    data = _get(f"/route/v1/driving/{_coords(points)}",
                {"overview": "full", "geometries": "geojson", "continue_straight": "true"})
    r = data["routes"][0]
    return {"coords": [[c[1], c[0]] for c in r["geometry"]["coordinates"]],
            "distance_m": r["distance"], "duration_s": r["duration"]}


def match(points: list[list[float]], timestamps: list[int] | None = None,
          radiuses: list[float] | None = None) -> dict:
    """Map-matching GPS-трека на дорожный граф. → {coords, distance_m, confidence}."""
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
    return {"coords": coords, "distance_m": dist, "confidence": min(conf) if conf else 0.0}
