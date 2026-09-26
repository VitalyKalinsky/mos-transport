"""Справочные данные: эталонное расписание, остановки, маршрутная сеть, привязка терминалов к ТС."""
from __future__ import annotations

import logging
import re
from bisect import bisect_right
from dataclasses import dataclass, field

import pandas as pd

from .geo import haversine_m

log = logging.getLogger("backend.reference")

_POINT = re.compile(r"POINT \(([-\d.]+) ([-\d.]+)\)")


@dataclass(slots=True)
class PlannedStop:
    """Плановое прибытие ТС на остановку (строка расписания)."""
    item_id: int            # tt_action_item_id (= target_stop_id)
    plan_ts: float          # плановое время, Unix-сек UTC
    lon: float
    lat: float
    stop_key: str           # физическая остановка (по координатам)
    address: str


@dataclass
class VehicleSchedule:
    tr_id: int
    stops: list[PlannedStop]
    plan_ts: list[float] = field(default_factory=list)
    route_id: str = ""

    def __post_init__(self) -> None:
        self.plan_ts = [s.plan_ts for s in self.stops]

    def last_planned_before(self, ts: float) -> int:
        """Индекс последней остановки с плановым временем ≤ ts (или -1)."""
        return bisect_right(self.plan_ts, ts) - 1

    def first_in_window(self, lo: float, hi: float) -> int:
        """Индекс первой остановки с плановым временем в (lo, hi] (или -1)."""
        i = bisect_right(self.plan_ts, lo)
        if i < len(self.stops) and self.plan_ts[i] <= hi:
            return i
        return -1


@dataclass
class Reference:
    schedules: dict[int, VehicleSchedule]
    unit_to_tr: dict[int, int]
    stops: dict[str, dict]                     # stop_key -> {lon, lat, address, routes}
    routes: dict[str, dict]                    # route_id -> {vehicles, stops, segments}
    segments: dict[str, dict]                  # seg_id -> {a, b, coords, routes}

    def tr_for_unit(self, unit_id: int) -> int | None:
        return self.unit_to_tr.get(unit_id)


def _to_ts(s: pd.Series) -> pd.Series:
    """Наивные метки времени датасета — UTC (см. sample_id = tr_id_<unix T>)."""
    return (pd.to_datetime(s, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()


def seg_id(a: str, b: str) -> str:
    return f"{a}|{b}" if a <= b else f"{b}|{a}"


def load_reference(schedule_path: str, units_path: str) -> Reference:
    sch = pd.read_csv(schedule_path)
    xy = sch["geom"].str.extract(_POINT).astype(float)
    sch["lon"], sch["lat"] = xy[0], xy[1]
    sch["plan_ts"] = _to_ts(sch["time_begin"])
    sch["stop_key"] = sch["lon"].round(6).astype(str) + "," + sch["lat"].round(6).astype(str)
    # у части остановок адрес не заполнен — подставляем координаты, чтобы диспетчер мог её найти
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

    # Привязка терминал (unit_id из NPL peerAddress) → ТС (tr_id)
    units = pd.read_csv(units_path, usecols=["unit_id", "tr_id"]).drop_duplicates()
    unit_to_tr = {int(u): int(t) for u, t in zip(units["unit_id"], units["tr_id"])}

    routes, segments = _build_network(schedules, stops)
    log.info("reference: %d scheduled vehicles, %d planned stops, %d physical stops, %d routes, "
             "%d segments, %d known units", len(schedules), len(sch), len(stops), len(routes),
             len(segments), len(unit_to_tr))
    return Reference(schedules, unit_to_tr, stops, routes, segments)


def _build_network(schedules: dict[int, VehicleSchedule], stops: dict[str, dict]):
    """Маршрутная сеть: ТС с общими остановками объединяются в маршрут (union-find по
    доле общих остановок), сегменты = пары последовательных остановок по расписанию."""
    stop_sets = {tr: {s.stop_key for s in sch.stops} for tr, sch in schedules.items()}
    parent = {tr: tr for tr in schedules}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    trs = list(schedules)
    for i, a in enumerate(trs):
        for b in trs[i + 1:]:
            inter = len(stop_sets[a] & stop_sets[b])
            if inter and inter / min(len(stop_sets[a]), len(stop_sets[b])) >= 0.5:
                parent[find(a)] = find(b)

    groups: dict[int, list[int]] = {}
    for tr in trs:
        groups.setdefault(find(tr), []).append(tr)
    ordered = sorted(groups.values(), key=lambda v: (-len(v), min(v)))

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
                    # разрыв > 30 мин или > 3 км — это межрейсовый перегон/отстой, не сегмент маршрута
                    if s.plan_ts - prev.plan_ts <= 1800 and haversine_m(prev.lon, prev.lat, s.lon, s.lat) <= 3000:
                        sid = seg_id(prev.stop_key, s.stop_key)
                        seg = segments.setdefault(sid, {
                            "id": sid, "a": prev.stop_key, "b": s.stop_key,
                            "coords": [[prev.lat, prev.lon], [s.lat, s.lon]], "routes": set(),
                        })
                        seg["routes"].add(rid)
                        rsegs.add(sid)
                prev = s
        routes[rid] = {"id": rid, "vehicles": sorted(members), "stops": sorted(rstops),
                       "segments": sorted(rsegs)}
    return routes, segments
