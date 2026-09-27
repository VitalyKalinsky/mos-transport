"""Тесты расширений: геометрия, ETA (в т.ч. счисление пути при обрыве), what-if, реестр маршрутов, экспорт."""
from __future__ import annotations

import asyncio
import csv
import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "backend"))
sys.path.insert(0, str(ROOT / "libs"))

from app.config import Settings  # noqa: E402
from app.eta import EtaEngine, Overrides  # noqa: E402
from app.geometry import Polyline, remove_spurs, simplify  # noqa: E402
from app.reference import PlannedStop, Reference, VehicleSchedule, seg_id  # noqa: E402
from app.routes_registry import import_gtfs, stitch  # noqa: E402
from app.tracker import VehicleState  # noqa: E402

CFG = Settings()
T0 = 1767690000.0
DLON = 400 / (111320 * 0.5628)   # ≈400 м на широте Москвы


def make_ref(n=12, step_s=60, layover_after=None):
    stops = []
    t = T0
    for i in range(n):
        if layover_after is not None and i == layover_after + 1:
            t += 900                                   # межрейсовый отстой 15 мин
        stops.append(PlannedStop(1000 + i, t, 37.5 + i * DLON, 55.75, f"k{i}", f"Остановка {i}"))
        t += step_s
    sch = VehicleSchedule(1, stops)
    ref = Reference({1: sch}, {1: 1}, {}, {}, {}, {})
    return ref, sch


def drive(st, sch, delay_s, until_idx=None, dt=12, pace=1.0):
    end = sch.stops[until_idx if until_idx is not None else -1].plan_ts + delay_s
    t = T0 + delay_s - 20
    while t < end:
        x = max(0.0, (t - T0 - delay_s) / 60 / pace) * DLON
        st.ingest(t, 37.5 + x, 55.75, True, 20.0, 90.0, 150.0, False)
        t += dt
    return t


# ------------------------------------------------------------------ geometry
def test_polyline_project_and_point_at():
    pl = Polyline([[55.75, 37.5], [55.75, 37.5 + DLON], [55.75, 37.5 + 2 * DLON]])
    assert pl.length == pytest.approx(800, rel=0.01)
    s, off = pl.project(55.7501, 37.5 + 1.5 * DLON)
    assert s == pytest.approx(600, rel=0.02) and off < 15
    lat, lon = pl.point_at(200)
    assert lon == pytest.approx(37.5 + DLON / 2, rel=1e-6)


def test_simplify_and_spurs():
    line = [[55.75, 37.5 + i * DLON / 10] for i in range(11)]
    assert len(simplify(line, 1.0)) == 2
    spur = [[55.75, 37.5], [55.75, 37.5 + DLON / 4], [55.7506, 37.5 + DLON / 4], [55.75001, 37.5 + DLON / 4 + 1e-6],
            [55.75, 37.5 + DLON]]
    cleaned = remove_spurs(spur, close_m=15)
    assert len(cleaned) < len(spur) and cleaned[-1] == spur[-1]


def test_stitch_orients_ways():
    a = [[55.0, 37.0], [55.0, 37.1]]
    b = [[55.0, 37.2], [55.0, 37.1]]                    # развёрнут
    c = [[55.0, 37.2], [55.0, 37.3]]
    line = stitch([a, b, c])
    assert line[0] == [55.0, 37.0] and line[-1] == [55.0, 37.3]
    assert [p[1] for p in line] == sorted(p[1] for p in line)


# ------------------------------------------------------------------ ETA
def test_eta_live_follows_delay():
    ref, sch = make_ref()
    st = VehicleState(1, 1, sch, CFG)
    drive(st, sch, delay_s=120, until_idx=4)
    eng = EtaEngine(ref)
    res = eng.predict(st, st.last_ts, 5)
    assert res.mode == "live" and res.stops
    # ближайшая остановка: опоздание ≈ 2 мин (затухание к типичному уровню незначительно на 1–2 мин горизонта)
    assert res.stops[0].delay_s == pytest.approx(120, abs=45)


def test_eta_dead_reckoning_moves_vehicle():
    ref, sch = make_ref(n=20)
    st = VehicleState(1, 1, sch, CFG)
    drive(st, sch, delay_s=0, until_idx=5)
    eng = EtaEngine(ref)
    T = st.last_ts + 180                                 # связи нет 3 минуты
    res = eng.predict(st, T, 5)
    assert res.mode == "dead_reckoning"
    assert res.est_lon > st.lon + DLON                    # расчётное положение ушло вперёд по маршруту
    assert all(e.eta_ts >= T for e in res.stops)


def test_eta_layover_absorbs_delay():
    ref, sch = make_ref(n=12, layover_after=5)
    st = VehicleState(1, 1, sch, CFG)
    drive(st, sch, delay_s=180, until_idx=3)
    raw = EtaEngine(ref)._predict(st, st.last_ts, 10, None, raw=True)
    after = [e for e in raw.stops if e.idx > 5]
    assert after and after[0].delay_s < 60              # отстой 15 мин «съедает» 3 мин опоздания


def test_eta_overrides_breakdown_and_skip():
    ref, sch = make_ref(n=20)
    st = VehicleState(1, 1, sch, CFG)
    drive(st, sch, delay_s=0, until_idx=4)
    eng = EtaEngine(ref)
    T = st.last_ts
    base = eng.predict(st, T, 8)
    ov = Overrides(extra_delay={1: (T, 600)})
    broken = eng.predict(st, T, 8, ov)
    assert broken.stops[0].eta_ts - base.stops[0].eta_ts == pytest.approx(600, abs=60)
    ov2 = Overrides(segment_factor={seg_id(sch.stops[i].stop_key, sch.stops[i + 1].stop_key): 0.5 for i in range(19)})
    faster = eng.predict(st, T, 8, ov2)
    assert faster.stops[-1].eta_ts <= base.stops[-1].eta_ts


def test_bus_lane_reduces_jam():
    ref, _ = make_ref()
    eng = EtaEngine(ref)
    eng.manual_traffic["x"] = 1.96
    no_lane = eng.traffic_factor("x", 1, T0)
    eng.bus_lanes["x"] = 1.0
    assert eng.traffic_factor("x", 1, T0) == pytest.approx(1 + 0.96 * 0.3) and no_lane == pytest.approx(1.96)


# ------------------------------------------------------------------ what-if (через движок)
class _Clock:
    def now(self):
        return self.T


class _Engine:
    def __init__(self, ref, st):
        self.ref, self.eta = ref, EtaEngine(ref)
        self.vehicles = {st.unit_id: st}
        self.live_overrides = None
        self.route_names = {}
        self.clock = _Clock()

    def _local(self, ts):
        return str(int(ts))


@pytest.mark.parametrize("kind,params,expect", [
    ("breakdown", {"minutes": 10}, "worse"),
    ("signal_priority", {"gain": 0.2}, "better"),
    ("skip_stops", {"count": 3}, "better"),
    ("traffic", {"jam": 8}, "worse"),
])
def test_whatif_directions(kind, params, expect):
    from app.whatif import WhatIf
    ref, sch = make_ref(n=30)
    st = VehicleState(1, 1, sch, CFG)
    drive(st, sch, delay_s=200, until_idx=4)
    eng = _Engine(ref, st)
    eng.clock.T = st.last_ts
    res = asyncio.run(WhatIf(eng).simulate([{"type": kind, "params": {"unit_id": 1, **params}}], 12))
    b, a = res["totals"]["before"]["delay_min"], res["totals"]["after"]["delay_min"]
    assert (a > b) if expect == "worse" else (a < b), (kind, b, a)


# ------------------------------------------------------------------ GTFS import
def test_gtfs_import_with_shapes():
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")

    def w(name, rows):
        s = io.StringIO()
        csv.writer(s).writerows(rows)
        z.writestr(name, s.getvalue())
    w("routes.txt", [["route_id", "route_short_name", "route_long_name", "route_type"], ["r1", "м6", "Тест", "3"]])
    w("trips.txt", [["route_id", "service_id", "trip_id", "direction_id", "shape_id"], ["r1", "d", "t1", "0", "sh1"]])
    w("stops.txt", [["stop_id", "stop_name", "stop_lat", "stop_lon"], ["a", "A", "55.75", "37.60"], ["b", "B", "55.76", "37.61"]])
    w("stop_times.txt", [["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                         ["t1", "08:00:00", "08:00:00", "a", "1"], ["t1", "08:05:00", "08:05:00", "b", "2"]])
    w("shapes.txt", [["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
                     ["sh1", "55.75", "37.60", "1"], ["sh1", "55.755", "37.60", "2"], ["sh1", "55.76", "37.61", "3"]])
    z.close()
    routes = import_gtfs(buf.getvalue())
    assert len(routes) == 1 and routes[0]["ref"] == "м6" and len(routes[0]["stops"]) == 2 and len(routes[0]["geometry"]) == 3


# ------------------------------------------------------------------ export
def test_gtfs_rt_protobuf_roundtrip():
    from app import export
    if export.gtfs_realtime_pb2 is None:
        pytest.skip("gtfs-realtime-bindings not installed")
    d = {"header": {"gtfs_realtime_version": "2.0", "incrementality": "FULL_DATASET", "timestamp": 1},
         "entity": [{"id": "vp-1", "vehicle": {"vehicle": {"id": "1"}, "position": {"latitude": 55.7, "longitude": 37.6},
                                               "timestamp": 1}}]}
    raw = export.to_protobuf(d)
    msg = export.gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(raw)
    assert msg.entity[0].vehicle.position.latitude == pytest.approx(55.7)
