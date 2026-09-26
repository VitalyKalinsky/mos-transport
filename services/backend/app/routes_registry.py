"""Реестр реальных маршрутов: импорт, хранение, привязка ТС.

Источники (как добавлять реальные маршруты):
  1. OpenStreetMap по номеру маршрута или ID relation (Overpass API; маршруты Москвы в OSM размечены
     по схеме PTv2 — остановки и геометрия по дорогам). POST /api/routes/import/osm
  2. GTFS (стандарт публикации расписаний; его же принимают Яндекс/2ГИС). POST /api/routes/import/gtfs
  3. Вручную: список остановок → геометрия по дорогам строится роутером. POST /api/routes
Маршрут хранится JSON-файлом в ROUTES_DIR (docker volume), встроенные — в data/routes.
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

OVERPASS_MIRRORS = [m for m in os.getenv("OVERPASS_URLS", ",".join([
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
])).split(",") if m]
MODES = {"bus": "Автобус", "trolleybus": "Троллейбус", "tram": "Трамвай", "share_taxi": "Маршрутное такси"}
COLORS = ["#2a78d6", "#1baf7a", "#9085e9", "#e87ba4", "#eda100", "#199e70", "#4a3aa7", "#d95926"]


class ImportError_(RuntimeError):
    pass


def overpass(query: str, timeout: float = 90) -> dict:
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
                time.sleep(1.5 * (attempt + 1))
    raise ImportError_("Overpass недоступен: " + "; ".join(errors))


def stitch(ways: list[list[list[float]]]) -> list[list[float]]:
    """Склейка геометрии путей relation в одну линию с разворотом сегментов по ближайшим концам."""
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
        line.extend(seg[1:] if min(d_fwd, d_rev) < 1 else seg)
    return line


def _osm_variant(rel: dict, names: dict[int, str]) -> dict:
    tags = rel.get("tags", {})
    stops, platforms, ways = [], [], []
    for m in rel.get("members", []):
        role = m.get("role", "")
        if m["type"] == "node" and role.startswith("stop"):
            stops.append({"name": names.get(m["ref"], ""), "lat": m["lat"], "lon": m["lon"], "osm_node": m["ref"]})
        elif m["type"] == "node" and role.startswith("platform"):
            platforms.append({"name": names.get(m["ref"], ""), "lat": m["lat"], "lon": m["lon"], "osm_node": m["ref"]})
        elif m["type"] == "way" and role in ("", "forward", "backward") and m.get("geometry"):
            ways.append([[g["lat"], g["lon"]] for g in m["geometry"]])
    if not stops:
        stops = platforms
    # имена стоп-позиций часто пусты — берём имя ближайшей платформы
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
    """Импорт маршрута из OSM. По номеру ищем сначала в административной границе города (area),
    затем — в границах региона (в области у многих городов свои маршруты с тем же номером)."""
    if relation_id:
        sels = [f"rel({int(relation_id)})"]
    elif ref:
        safe = re.sub(r'["\\]', "", ref.strip())
        flt = f'["type"="route"]["route"~"^(bus|trolleybus|tram|share_taxi)$"]["ref"="{safe}"]'
        (s, w), (n, e) = bbox
        sels = ([f'area["name"="{area}"]["boundary"="administrative"]->.a;rel(area.a){flt}'] if area else []) \
            + [f"rel{flt}({s},{w},{n},{e})"]
    else:
        raise ImportError_("укажите ref (номер маршрута) или relation_id")
    data = {"elements": []}
    for sel in sels:
        data = overpass(f"[out:json][timeout:80];{sel}->.r;.r out body geom;node(r.r);out tags;")
        if any(e["type"] == "relation" for e in data["elements"]):
            break
    names = {e["id"]: e.get("tags", {}).get("name", "") for e in data["elements"] if e["type"] == "node"}
    rels = [e for e in data["elements"] if e["type"] == "relation"]
    if not rels:
        raise ImportError_(f"маршрут «{ref or relation_id}» не найден в OSM в границах региона")
    return [_osm_variant(r, names) for r in rels]


def geometry_by_router(stops: list[dict]) -> tuple[list[list[float]], str]:
    """Геометрия по дорогам через остановки (кусками по 25 точек — ограничение публичного OSRM)."""
    pts = [[s["lat"], s["lon"]] for s in stops]
    line: list[list[float]] = []
    try:
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
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise ImportError_("файл не является ZIP-архивом GTFS") from e

    def table(name: str) -> list[dict]:
        if name not in z.namelist():
            return []
        with z.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

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
                geom, _ = geometry_by_router(rstops)
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
    def __init__(self, routes_dir: str, builtin_dir: str) -> None:
        self.dir = Path(routes_dir)
        self.builtin = Path(builtin_dir)
        self.routes: dict[str, dict] = {}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("routes dir %s not writable: %s", self.dir, e)
        for d in (self.builtin, self.dir):
            if d.exists():
                for f in sorted(d.glob("*.json")):
                    try:
                        r = json.loads(f.read_text("utf-8"))
                        r.setdefault("builtin", d == self.builtin)
                        self.routes[r["id"]] = r
                    except Exception as e:  # noqa: BLE001
                        log.warning("bad route file %s: %s", f, e)
        log.info("route registry: %d routes", len(self.routes))

    def _color(self, ref: str) -> str:
        return COLORS[sum(map(ord, ref or "x")) % len(COLORS)]

    def save(self, r: dict) -> dict:
        r.setdefault("vehicles", [])
        r.setdefault("color", self._color(r.get("ref", "")))
        r["updated"] = time.time()
        if r.get("geometry") and len(r["geometry"]) > 1:
            r["length_km"] = round(Polyline(r["geometry"]).length / 1000, 2)
        self.routes[r["id"]] = r
        try:
            (self.dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', r['id'])}.json").write_text(
                json.dumps(r, ensure_ascii=False), "utf-8")
        except OSError as e:
            log.warning("cannot persist route %s: %s", r["id"], e)
        return r

    def delete(self, rid: str) -> bool:
        r = self.routes.pop(rid, None)
        if r is None:
            return False
        f = self.dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', rid)}.json"
        if f.exists():
            f.unlink()
        return True

    def summary(self, r: dict) -> dict:
        return {k: r.get(k) for k in ("id", "ref", "name", "mode", "from", "to", "source", "vehicles",
                                      "color", "length_km", "builtin")} | {"stops": len(r.get("stops", []))}

    def vehicle_links(self) -> dict[int, dict]:
        links = {}
        for r in self.routes.values():
            for tr in r.get("vehicles", []):
                links[int(tr)] = {"id": r["id"], "ref": r.get("ref") or r["id"], "name": r.get("name", "")}
        return links

    def search(self, q: str) -> list[dict]:
        q = q.strip().lower()
        return [self.summary(r) for r in self.routes.values()
                if q and (q == (r.get("ref") or "").lower() or q in (r.get("name") or "").lower())]


def score_vehicle_route(stop_pts: list[tuple[float, float]], variant: dict) -> float:
    """Доля остановок ТС в пределах 40 м от линии маршрута OSM (для автопривязки)."""
    if len(variant.get("geometry", [])) < 2:
        return 0.0
    pl = Polyline(variant["geometry"])
    hit = sum(1 for lat, lon in stop_pts if pl.project(lat, lon)[1] <= 40)
    return hit / max(1, len(stop_pts))


def candidates_near(bbox: list[list[float]]) -> list[dict]:
    (s, w), (n, e) = bbox
    q = (f'[out:json][timeout:90];rel["type"="route"]["route"~"^(bus|trolleybus|tram)$"]({s},{w},{n},{e})->.r;'
         f".r out body geom;node(r.r);out tags;")
    data = overpass(q, timeout=120)
    names = {e["id"]: e.get("tags", {}).get("name", "") for e in data["elements"] if e["type"] == "node"}
    return [_osm_variant(r, names) for r in data["elements"] if r["type"] == "relation"]
