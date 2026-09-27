"""Реестр реальных маршрутов: импорт, хранение, привязка ТС.

Источники:

1. OpenStreetMap по номеру маршрута или ID relation (Overpass API). Маршруты Москвы в OSM
   размечены по PTv2: остановки + геометрия по дорогам. ``POST /api/routes/import/osm``
2. GTFS — стандарт публикации расписаний, его же принимают Яндекс и 2ГИС. ``POST /api/routes/import/gtfs``
3. Вручную: список остановок → геометрия по дорогам через роутер. ``POST /api/routes``

Маршрут хранится JSON-файлом в ``ROUTES_DIR`` (docker volume), встроенные — в ``data/routes``.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import routing
from .geo import haversine_m
from .geometry import Polyline, simplify

log = logging.getLogger("backend.routes")

# несколько зеркал: публичные Overpass часто отвечают 429/504; maps.mail.ru — российское зеркало
OVERPASS_MIRRORS = [m for m in os.getenv("OVERPASS_URLS", ",".join([
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
])).split(",") if m]
MODES = {"bus": "Автобус", "trolleybus": "Троллейбус", "tram": "Трамвай", "share_taxi": "Маршрутное такси"}
COLORS = ["#2a78d6", "#1baf7a", "#9085e9", "#e87ba4", "#eda100", "#199e70", "#4a3aa7", "#d95926"]


class ImportError_(RuntimeError):
    """Ошибка импорта маршрута; текст показывается диспетчеру как есть (HTTP 400/502)."""


def overpass(query: str, timeout: float = 90) -> dict:
    """Выполняет запрос Overpass QL с перебором зеркал.

    Args:
        query: Запрос Overpass QL.
        timeout: Таймаут одного HTTP-запроса, с.

    Returns:
        dict: JSON-ответ Overpass (``elements``).

    Raises:
        ImportError_: Все зеркала недоступны (по 2 попытки на зеркало).
    """
    errors = []
    for url in OVERPASS_MIRRORS:
        for attempt in range(2):
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "mos-transport-dispatcher/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = r.read()
                return json.loads(body)
            except Exception as e:  # noqa: BLE001 — зеркала Overpass нестабильны, пробуем следующее
                errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
                time.sleep(1.5 * (attempt + 1))  # линейный backoff: 429 от Overpass снимается за секунды
    raise ImportError_("Overpass недоступен: " + "; ".join(errors))


def stitch(ways: list[list[list[float]]]) -> list[list[float]]:
    """Склеивает пути relation в одну линию.

    В OSM ways маршрута направлены как угодно: каждый следующий разворачивается так, чтобы его
    ближайший конец примыкал к концу линии.

    Args:
        ways: Геометрии путей ``[[lat, lon], ...]`` в порядке членов relation.

    Returns:
        list[list[float]]: Непрерывная линия.
    """
    line: list[list[float]] = []
    for w in ways:
        if not w:
            continue
        if not line:
            line = list(w)
            continue
        end = line[-1]
        d_fwd = haversine_m(end[1], end[0], w[0][1], w[0][0])
        d_rev = haversine_m(end[1], end[0], w[-1][1], w[-1][0])
        if len(line) >= 2 and len(line) == len(ways[0]):   # первый путь мог быть ориентирован наоборот
            d_first_rev = min(haversine_m(line[0][1], line[0][0], w[0][1], w[0][0]),
                              haversine_m(line[0][1], line[0][0], w[-1][1], w[-1][0]))
            if d_first_rev < min(d_fwd, d_rev):
                line.reverse()
                end = line[-1]
                d_fwd = haversine_m(end[1], end[0], w[0][1], w[0][0])
                d_rev = haversine_m(end[1], end[0], w[-1][1], w[-1][0])
        seg = w if d_fwd <= d_rev else list(reversed(w))
        line.extend(seg[1:] if min(d_fwd, d_rev) < 1 else seg)  # общий узел (< 1 м) не дублируем
    return line


def _osm_variant(rel: dict, names: dict[int, str]) -> dict:
    """Relation OSM (одно направление маршрута) → запись реестра."""
    tags = rel.get("tags", {})
    stops, platforms, ways = [], [], []
    for m in rel.get("members", []):
        role = m.get("role", "")
        if m["type"] == "node" and role.startswith("stop"):
            stops.append({"name": names.get(m["ref"], ""), "lat": m["lat"], "lon": m["lon"], "osm_node": m["ref"]})
        elif m["type"] == "node" and role.startswith("platform"):
            platforms.append({"name": names.get(m["ref"], ""), "lat": m["lat"], "lon": m["lon"], "osm_node": m["ref"]})
        elif m["type"] == "way" and role in ("", "forward", "backward") and m.get("geometry"):
            ways.append([[g["lat"], g["lon"]] for g in m["geometry"]])  # роль platform у way — не путь следования
    if not stops:  # старая схема PTv1: есть только платформы
        stops = platforms
    # имена стоп-позиций часто пусты — берём имя ближайшей платформы (≤ 80 м)
    for s in stops:
        if not s["name"] and platforms:
            p = min(platforms, key=lambda p: haversine_m(s["lon"], s["lat"], p["lon"], p["lat"]))
            if haversine_m(s["lon"], s["lat"], p["lon"], p["lat"]) < 80:
                s["name"] = p["name"]
    geom = [[round(c[0], 6), round(c[1], 6)] for c in simplify(stitch(ways), 2.0)]
    return {
        "id": f"osm-{rel['id']}", "ref": tags.get("ref", ""), "name": tags.get("name", ""),
        "mode": tags.get("route", "bus"), "from": tags.get("from", ""), "to": tags.get("to", ""),
        "operator": tags.get("operator", ""), "source": "osm", "osm_relation": rel["id"],
        "stops": stops, "geometry": geom, "vehicles": [],
    }


def import_osm(ref: str | None, relation_id: int | None, bbox: list[list[float]],
               area: str | None = "Москва") -> list[dict]:
    """Импортирует маршрут из OSM (все направления).

    По номеру ищет сначала в административной границе города (``area``), затем в границах региона:
    в области у многих городов свои маршруты с тем же номером.

    Args:
        ref: Номер маршрута как в OSM («м6», «т25», «297»).
        relation_id: ID relation; приоритетнее ``ref``.
        bbox: Границы региона ``[[south, west], [north, east]]``.
        area: Название административной границы города или ``None``.

    Returns:
        list[dict]: Варианты маршрута (обычно два направления).

    Raises:
        ImportError_: Не заданы ни ``ref``, ни ``relation_id``; маршрут не найден; Overpass недоступен.
    """
    if relation_id:
        sels = [f"rel({int(relation_id)})"]
    elif ref:
        safe = re.sub(r'["\\]', "", ref.strip())  # защита от инъекции в Overpass QL
        flt = f'["type"="route"]["route"~"^(bus|trolleybus|tram|share_taxi)$"]["ref"="{safe}"]'
        (s, w), (n, e) = bbox
        sels = ([f'area["name"="{area}"]["boundary"="administrative"]->.a;rel(area.a){flt}'] if area else []) \
            + [f"rel{flt}({s},{w},{n},{e})"]
    else:
        raise ImportError_("укажите ref (номер маршрута) или relation_id")
    data = {"elements": []}
    for sel in sels:
        # out geom — геометрия путей сразу в ответе, без второго запроса за узлами
        data = overpass(f"[out:json][timeout:80];{sel}->.r;.r out body geom;node(r.r);out tags;")
        if any(e["type"] == "relation" for e in data["elements"]):
            break
    names = {e["id"]: e.get("tags", {}).get("name", "") for e in data["elements"] if e["type"] == "node"}
    rels = [e for e in data["elements"] if e["type"] == "relation"]
    if not rels:
        raise ImportError_(f"маршрут «{ref or relation_id}» не найден в OSM в границах региона")
    return [_osm_variant(r, names) for r in rels]


def geometry_by_router(stops: list[dict]) -> tuple[list[list[float]], str]:
    """Геометрия по дорогам через остановки.

    Args:
        stops: Остановки с ``lat``/``lon`` в порядке следования.

    Returns:
        tuple[list, str]: ``(геометрия, "router")``; если роутер недоступен —
        ``(прямые между остановками, "straight")`` без исключения.
    """
    pts = [[s["lat"], s["lon"]] for s in stops]
    line: list[list[float]] = []
    try:
        # кусками по 25 точек (лимит публичного OSRM), шаг 24 — соседние куски делят остановку
        for i in range(0, len(pts) - 1, 24):
            chunk = pts[i:i + 25]
            if len(chunk) < 2:
                break
            r = routing.route(chunk)
            line.extend(r["coords"] if not line else r["coords"][1:])
        return [[round(c[0], 6), round(c[1], 6)] for c in simplify(line, 2.0)], "router"
    except routing.RoutingError as e:
        log.warning("router unavailable (%s) — straight geometry", e)
        return pts, "straight"


def import_gtfs(blob: bytes) -> list[dict]:
    """Импортирует маршруты из GTFS-архива перевозчика.

    Для каждого маршрута и направления берётся рейс с наибольшим числом остановок (полный вариант).

    Args:
        blob: Содержимое zip-архива GTFS.

    Returns:
        list[dict]: Записи реестра (по одной на маршрут и направление).

    Raises:
        ImportError_: Не zip; нет обязательных таблиц; нет ни одного рейса.
    """
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise ImportError_("файл не является ZIP-архивом GTFS") from e

    def table(name: str) -> list[dict]:
        if name not in z.namelist():
            return []
        with z.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))  # utf-8-sig: BOM из Excel

    for req in ("routes.txt", "trips.txt", "stop_times.txt", "stops.txt"):
        if req not in z.namelist():
            raise ImportError_(f"в архиве нет {req}")
    stops = {s["stop_id"]: s for s in table("stops.txt")}
    trips = table("trips.txt")
    times: dict[str, list[dict]] = {}
    for st in table("stop_times.txt"):
        times.setdefault(st["trip_id"], []).append(st)
    shapes: dict[str, list] = {}
    for p in table("shapes.txt"):
        shapes.setdefault(p["shape_id"], []).append(p)
    # базовые и расширенные (7xx/8xx/9xx) коды route_type
    gtfs_type = {"0": "tram", "3": "bus", "11": "trolleybus", "800": "trolleybus", "700": "bus", "900": "tram"}
    out = []
    for r in table("routes.txt"):
        rtrips = [t for t in trips if t["route_id"] == r["route_id"]]
        for direction in sorted({t.get("direction_id", "0") for t in rtrips}):
            cand = [t for t in rtrips if t.get("direction_id", "0") == direction]
            best = max(cand, key=lambda t: len(times.get(t["trip_id"], [])), default=None)
            if best is None:
                continue
            seq = sorted(times.get(best["trip_id"], []), key=lambda x: int(x["stop_sequence"]))
            rstops = [{"name": stops[x["stop_id"]].get("stop_name", ""), "lat": float(stops[x["stop_id"]]["stop_lat"]),
                       "lon": float(stops[x["stop_id"]]["stop_lon"]), "gtfs_stop_id": x["stop_id"]}
                      for x in seq if x["stop_id"] in stops]
            shp = sorted(shapes.get(best.get("shape_id", ""), []), key=lambda p: int(p["shape_pt_sequence"]))
            if shp:
                geom = [[float(p["shape_pt_lat"]), float(p["shape_pt_lon"])] for p in shp]
            else:
                geom, _ = geometry_by_router(rstops)  # shapes.txt необязателен в GTFS
            out.append({
                "id": f"gtfs-{r['route_id']}-{direction}", "ref": r.get("route_short_name", ""),
                "name": r.get("route_long_name", "") or best.get("trip_headsign", ""),
                "mode": gtfs_type.get(r.get("route_type", "3"), "bus"),
                "from": rstops[0]["name"] if rstops else "", "to": rstops[-1]["name"] if rstops else "",
                "operator": "", "source": "gtfs", "stops": rstops, "geometry": geom, "vehicles": [],
            })
    if not out:
        raise ImportError_("в GTFS не найдено ни одного рейса")
    return out


class RouteRegistry:
    """Хранилище реальных маршрутов (JSON-файлы) с привязкой ТС.

    Встроенные маршруты (``builtin_dir``, в образе) загружаются первыми; пользовательские
    (``routes_dir``, volume) с тем же ID их перекрывают.

    Args:
        routes_dir: Каталог пользовательских маршрутов (должен быть доступен на запись).
        builtin_dir: Каталог встроенных маршрутов (только чтение).
    """

    def __init__(self, routes_dir: str, builtin_dir: str) -> None:
        self.dir = Path(routes_dir)
        self.builtin = Path(builtin_dir)
        self.routes: dict[str, dict] = {}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # read-only FS не должна ронять сервис: реестр работает в памяти
            log.warning("routes dir %s not writable: %s", self.dir, e)
        for d in (self.builtin, self.dir):
            if d.exists():
                for f in sorted(d.glob("*.json")):
                    try:
                        r = json.loads(f.read_text("utf-8"))
                        r.setdefault("builtin", d == self.builtin)
                        self.routes[r["id"]] = r
                    except Exception as e:  # noqa: BLE001 — один битый файл не ломает реестр
                        log.warning("bad route file %s: %s", f, e)
        log.info("route registry: %d routes", len(self.routes))

    def _color(self, ref: str) -> str:
        return COLORS[sum(map(ord, ref or "x")) % len(COLORS)]  # детерминированный цвет по номеру

    def save(self, r: dict) -> dict:
        """Сохраняет маршрут в памяти и на диске.

        Args:
            r: Запись маршрута (обязателен ``id``).

        Returns:
            dict: Та же запись с ``color``, ``updated``, ``length_km``. Ошибка записи на диск только логируется.
        """
        r.setdefault("vehicles", [])
        r.setdefault("color", self._color(r.get("ref", "")))
        r["updated"] = time.time()
        if r.get("geometry") and len(r["geometry"]) > 1:
            r["length_km"] = round(Polyline(r["geometry"]).length / 1000, 2)
        self.routes[r["id"]] = r
        try:
            # ID из внешних источников — чистим до безопасного имени файла
            (self.dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', r['id'])}.json").write_text(
                json.dumps(r, ensure_ascii=False), "utf-8")
        except OSError as e:
            log.warning("cannot persist route %s: %s", r["id"], e)
        return r

    def delete(self, rid: str) -> bool:
        """Удаляет маршрут.

        Args:
            rid: ID маршрута.

        Returns:
            bool: ``False`` — маршрута нет.
        """
        r = self.routes.pop(rid, None)
        if r is None:
            return False
        f = self.dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', rid)}.json"
        if f.exists():
            f.unlink()
        return True

    def summary(self, r: dict) -> dict:
        """Краткая карточка маршрута для списков (без геометрии)."""
        return {k: r.get(k) for k in ("id", "ref", "name", "mode", "from", "to", "source", "vehicles",
                                      "color", "length_km", "builtin")} | {"stops": len(r.get("stops", []))}

    def vehicle_links(self) -> dict[int, dict]:
        """Привязки ТС к реальным маршрутам.

        Returns:
            dict[int, dict]: tr_id → ``{id, ref, name}``; при нескольких привязках побеждает последняя.
        """
        links = {}
        for r in self.routes.values():
            for tr in r.get("vehicles", []):
                links[int(tr)] = {"id": r["id"], "ref": r.get("ref") or r["id"], "name": r.get("name", "")}
        return links

    def search(self, q: str) -> list[dict]:
        """Поиск по точному номеру или подстроке названия (без учёта регистра)."""
        q = q.strip().lower()
        return [self.summary(r) for r in self.routes.values()
                if q and (q == (r.get("ref") or "").lower() or q in (r.get("name") or "").lower())]


def score_vehicle_route(stop_pts: list[tuple[float, float]], variant: dict) -> float:
    """Доля остановок ТС в пределах 40 м от линии маршрута OSM (для автопривязки).

    Args:
        stop_pts: Остановки расписания ТС ``(lat, lon)``.
        variant: Вариант маршрута с ``geometry``.

    Returns:
        float: 0..1; 40 м ≈ полоса дороги + погрешность разметки OSM.
    """
    if len(variant.get("geometry", [])) < 2:
        return 0.0
    pl = Polyline(variant["geometry"])
    hit = sum(1 for lat, lon in stop_pts if pl.project(lat, lon)[1] <= 40)
    return hit / max(1, len(stop_pts))


def candidates_near(bbox: list[list[float]]) -> list[dict]:
    """Все маршруты OSM в прямоугольнике (кандидаты для автопривязки ТС).

    Args:
        bbox: ``[[south, west], [north, east]]``.

    Returns:
        list[dict]: Варианты маршрутов.

    Raises:
        ImportError_: Overpass недоступен.
    """
    (s, w), (n, e) = bbox
    q = (f'[out:json][timeout:90];rel["type"="route"]["route"~"^(bus|trolleybus|tram)$"]({s},{w},{n},{e})->.r;'
         f".r out body geom;node(r.r);out tags;")
    data = overpass(q, timeout=120)
    names = {e["id"]: e.get("tags", {}).get("name", "") for e in data["elements"] if e["type"] == "node"}
    return [_osm_variant(r, names) for r in data["elements"] if r["type"] == "relation"]
