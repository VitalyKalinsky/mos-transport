"""Справочные данные: эталонное расписание, остановки, маршрутная сеть, привязка терминалов к ТС.

Загружаются один раз при старте из CSV датасета и ``data/network/shapes.json``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from bisect import bisect_right
from dataclasses import dataclass, field

import pandas as pd

from .geo import haversine_m
from .geometry import Polyline, remove_spurs

log = logging.getLogger("backend.reference")

_POINT = re.compile(r"POINT \(([-\d.]+) ([-\d.]+)\)")  # WKT из колонки geom: POINT (lon lat)


@dataclass(slots=True)
class PlannedStop:
    """Плановое прибытие ТС на остановку (строка расписания)."""
    item_id: int            # tt_action_item_id (= target_stop_id в разметке)
    plan_ts: float          # плановое время, unix-с UTC
    lon: float
    lat: float
    stop_key: str           # физическая остановка по координатам: item_id уникален для каждого рейса
    address: str


@dataclass
class VehicleSchedule:
    """Нитка графика одного ТС на день.

    Attributes:
        tr_id: Идентификатор ТС.
        stops: Плановые прибытия, отсортированные по времени.
        plan_ts: Плановые времена (кэш для bisect).
        route_id: Маршрут сети (``M1``, ``M2``…), назначается в ``_build_network``.
    """

    tr_id: int
    stops: list[PlannedStop]
    plan_ts: list[float] = field(default_factory=list)
    route_id: str = ""

    def __post_init__(self) -> None:
        self.plan_ts = [s.plan_ts for s in self.stops]

    def last_planned_before(self, ts: float) -> int:
        """Последняя остановка с плановым временем ≤ ``ts``.

        Args:
            ts: Момент времени, unix-с.

        Returns:
            int: Индекс в ``stops`` или ``-1``, если такой нет.
        """
        return bisect_right(self.plan_ts, ts) - 1

    def first_in_window(self, lo: float, hi: float) -> int:
        """Первая остановка с плановым временем в ``(lo, hi]``; так выбирается цель прогноза T+10…15.

        Args:
            lo: Нижняя граница (не включительно), unix-с.
            hi: Верхняя граница (включительно), unix-с.

        Returns:
            int: Индекс в ``stops`` или ``-1``.
        """
        i = bisect_right(self.plan_ts, lo)
        if i < len(self.stops) and self.plan_ts[i] <= hi:
            return i
        return -1


@dataclass
class Reference:
    """Все справочники сервиса.

    Attributes:
        schedules: tr_id → нитка графика.
        unit_to_tr: unit_id терминала (NDTP peerAddress) → tr_id.
        stops: stop_key → ``{lon, lat, address, routes}``.
        routes: route_id → ``{vehicles, stops, segments}``.
        segments: seg_id → ``{a, b, coords, routes, source}``.
        shapes: Геометрия перегонов по дорогам (``shapes.json``).
    """

    schedules: dict[int, VehicleSchedule]
    unit_to_tr: dict[int, int]
    stops: dict[str, dict]
    routes: dict[str, dict]
    segments: dict[str, dict]
    shapes: dict[str, dict] = field(default_factory=dict)
    _poly: dict[str, Polyline] = field(default_factory=dict)   # ленивый кэш: Polyline строится при первом запросе

    def tr_for_unit(self, unit_id: int) -> int | None:
        """ТС по терминалу.

        Args:
            unit_id: Идентификатор терминала.

        Returns:
            int | None: tr_id или ``None`` для терминала без наряда (например, эмулятор).
        """
        return self.unit_to_tr.get(unit_id)

    def polyline(self, a: PlannedStop, b: PlannedStop) -> Polyline:
        """Геометрия перегона ``a → b``.

        Args:
            a: Начальная остановка.
            b: Конечная остановка.

        Returns:
            Polyline: Линия по дорогам; если геометрии нет — прямая между остановками.
        """
        sid = seg_id(a.stop_key, b.stop_key)
        pl = self._poly.get(sid)
        if pl is None:
            shp = self.shapes.get(sid)
            coords = shp["coords"] if shp else [[a.lat, a.lon], [b.lat, b.lon]]
            pl = self._poly[sid] = Polyline(coords)
        return pl


def _to_ts(s: pd.Series) -> pd.Series:
    """Наивные метки времени датасета → unix-с. Они в UTC: ``sample_id = tr_id_<unix T>``."""
    return (pd.to_datetime(s, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()


def seg_id(a: str, b: str) -> str:
    """Идентификатор направленного сегмента.

    Направление важно: встречные полосы и одностороннее движение дают разную геометрию.

    Args:
        a: stop_key начала.
        b: stop_key конца.

    Returns:
        str: ``"a>b"``.
    """
    return f"{a}>{b}"


def load_shapes(path: str | None = None) -> dict[str, dict]:
    """Загружает геометрию перегонов по дорогам (собрана ``scripts/build_route_shapes.py``).

    Args:
        path: Путь к ``shapes.json``; по умолчанию ``SHAPES_PATH``.

    Returns:
        dict[str, dict]: seg_id → ``{coords, source}``; пустой словарь, если файла нет
        (сеть рисуется прямыми, сервис работает).
    """
    path = path or os.getenv("SHAPES_PATH", "/app/data/network/shapes.json")
    try:
        with open(path, encoding="utf-8") as f:
            shapes = json.load(f)
        for v in shapes.values():
            v["coords"] = remove_spurs(v["coords"])  # усы у остановок на боковых проездах
        log.info("road-snapped shapes: %d segments from %s", len(shapes), path)
        return shapes
    except FileNotFoundError:
        log.warning("shapes file %s not found — segments drawn as straight lines", path)
        return {}


def load_reference(schedule_path: str, units_path: str, shapes_path: str | None = None) -> Reference:
    """Строит справочники из файлов датасета.

    Args:
        schedule_path: Плановое расписание (``schedule_plan.csv``: без факта, как в проде).
        units_path: CSV с колонками ``unit_id``, ``tr_id`` (привязка терминалов).
        shapes_path: Путь к ``shapes.json``.

    Returns:
        Reference: Расписание, сеть, остановки и привязки.

    Raises:
        FileNotFoundError: Нет файла расписания или привязки терминалов.
        KeyError: В CSV нет обязательных колонок.
    """
    sch = pd.read_csv(schedule_path)
    xy = sch["geom"].str.extract(_POINT).astype(float)
    sch["lon"], sch["lat"] = xy[0], xy[1]
    sch["plan_ts"] = _to_ts(sch["time_begin"])
    # округление до 6 знаков (~10 см) склеивает одну физическую остановку разных рейсов
    sch["stop_key"] = sch["lon"].round(6).astype(str) + "," + sch["lat"].round(6).astype(str)
    # у части остановок нет адреса — подставляем координаты, чтобы диспетчер мог её найти
    noaddr = sch["building_address"].isna() | (sch["building_address"].astype(str).str.strip() == "")
    sch["building_address"] = sch["building_address"].where(
        ~noaddr, "ост. " + sch["lat"].round(5).astype(str) + ", " + sch["lon"].round(5).astype(str))
    sch = sch.dropna(subset=["lon", "lat", "plan_ts"]).sort_values(["tr_id", "plan_ts"])

    schedules: dict[int, VehicleSchedule] = {}
    stops: dict[str, dict] = {}
    for tr_id, g in sch.groupby("tr_id", sort=False):
        items = [
            PlannedStop(int(r.tt_action_item_id), float(r.plan_ts), float(r.lon), float(r.lat),
                        r.stop_key, str(r.building_address))
            for r in g.itertuples(index=False)
        ]
        schedules[int(tr_id)] = VehicleSchedule(int(tr_id), items)
        for s in items:
            stops.setdefault(s.stop_key, {"key": s.stop_key, "lon": s.lon, "lat": s.lat,
                                          "address": s.address, "routes": set()})

    # NDTP несёт только peerAddress терминала; tr_id узнаём по таблице из телеметрии датасета
    units = pd.read_csv(units_path, usecols=["unit_id", "tr_id"]).drop_duplicates()
    unit_to_tr = {int(u): int(t) for u, t in zip(units["unit_id"], units["tr_id"])}

    shapes = load_shapes(shapes_path)
    routes, segments = _build_network(schedules, stops, shapes)
    log.info("reference: %d scheduled vehicles, %d planned stops, %d physical stops, %d routes, "
             "%d segments, %d known units", len(schedules), len(sch), len(stops), len(routes),
             len(segments), len(unit_to_tr))
    return Reference(schedules, unit_to_tr, stops, routes, segments, shapes)


def _build_network(schedules: dict[int, VehicleSchedule], stops: dict[str, dict], shapes: dict[str, dict]):
    """Восстанавливает маршрутную сеть из расписания (номеров маршрутов в датасете нет).

    ТС с долей общих остановок ≥ 50% объединяются в маршрут (union-find); сегменты —
    пары последовательных остановок нитки графика.

    Args:
        schedules: Нитки графика; ``route_id`` проставляется на месте.
        stops: Физические остановки; ``routes`` дополняется на месте.
        shapes: Геометрия перегонов по дорогам.

    Returns:
        tuple[dict, dict]: ``(routes, segments)``.
    """
    stop_sets = {tr: {s.stop_key for s in sch.stops} for tr, sch in schedules.items()}
    parent = {tr: tr for tr in schedules}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # сжатие пути
            x = parent[x]
        return x

    trs = list(schedules)
    for i, a in enumerate(trs):
        for b in trs[i + 1:]:
            inter = len(stop_sets[a] & stop_sets[b])
            # доля от меньшего набора: укороченный рейс того же маршрута тоже склеивается
            if inter and inter / min(len(stop_sets[a]), len(stop_sets[b])) >= 0.5:
                parent[find(a)] = find(b)

    groups: dict[int, list[int]] = {}
    for tr in trs:
        groups.setdefault(find(tr), []).append(tr)
    ordered = sorted(groups.values(), key=lambda v: (-len(v), min(v)))  # стабильные ID: M1 — самый большой

    routes: dict[str, dict] = {}
    segments: dict[str, dict] = {}
    for n, members in enumerate(ordered, start=1):
        rid = f"M{n}"
        rstops: set[str] = set()
        rsegs: set[str] = set()
        for tr in members:
            sch = schedules[tr]
            sch.route_id = rid
            prev = None
            for s in sch.stops:
                rstops.add(s.stop_key)
                stops[s.stop_key]["routes"].add(rid)
                if prev is not None and prev.stop_key != s.stop_key:
                    # разрыв > 30 мин или > 3 км — межрейсовый перегон или отстой, а не сегмент маршрута
                    if s.plan_ts - prev.plan_ts <= 1800 and haversine_m(prev.lon, prev.lat, s.lon, s.lat) <= 3000:
                        sid = seg_id(prev.stop_key, s.stop_key)
                        shp = shapes.get(sid)
                        seg = segments.setdefault(sid, {
                            "id": sid, "a": prev.stop_key, "b": s.stop_key,
                            "coords": shp["coords"] if shp else [[prev.lat, prev.lon], [s.lat, s.lon]],
                            "source": shp["source"] if shp else "straight", "routes": set(),
                        })
                        seg["routes"].add(rid)
                        rsegs.add(sid)
                prev = s
        routes[rid] = {"id": rid, "vehicles": sorted(members), "stops": sorted(rstops),
                       "segments": sorted(rsegs)}
    return routes, segments
