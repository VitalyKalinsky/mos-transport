"""Тесты логики Backend: сопоставление с расписанием, cur_dev_s, риск, часы данных, приём кадров."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "backend"))
sys.path.insert(0, str(ROOT / "libs"))

from app.clock import DataClock  # noqa: E402
from app.config import Settings  # noqa: E402
from app.reference import PlannedStop, VehicleSchedule  # noqa: E402
from app.risk import diagnose, laplace_sf, risk_level  # noqa: E402
from app.tracker import VehicleState  # noqa: E402

CFG = Settings()
T0 = 1767690000.0  # 2026-01-06 09:00 UTC


def line_schedule(n=10, step_m=400, step_s=60):
    """Прямая линия остановок на восток (~400 м) каждые 60 с."""
    dlon = step_m / (111320 * 0.5628)  # cos(55.75°)
    stops = [PlannedStop(1000 + i, T0 + i * step_s, 37.5 + i * dlon, 55.75, f"k{i}", f"Остановка {i}") for i in range(n)]
    return VehicleSchedule(1, stops), dlon


def drive(st, sch, dlon, delay_s, speed_mps=400 / 60, dt=12):
    """ТС идёт по линии с постоянной задержкой; точки GPS раз в dt секунд."""
    t = T0 + delay_s - 30
    arrivals = []
    while t < sch.stops[-1].plan_ts + delay_s:
        x = (t - T0 - delay_s) * speed_mps / 400 * dlon
        arrivals += st.ingest(t, 37.5 + max(0.0, x), 55.75, True, 24.0, 90.0, 150.0, False)
        t += dt
    return arrivals


def test_arrival_detection_with_sparse_gps():
    sch, dlon = line_schedule()
    st = VehicleState(1, 1, sch, CFG)
    arr = drive(st, sch, dlon, delay_s=90)
    assert len(arr) >= 9
    for a in arr:
        if a.idx > 0:   # на остановке 0 ТС стоит с начала симуляции — прибытие раньше
            assert a.delay_s == pytest.approx(90, abs=12)


def test_current_deviation_uses_last_planned_stop():
    sch, dlon = line_schedule()
    st = VehicleState(1, 1, sch, CFG)
    drive(st, sch, dlon, delay_s=120)
    dev, method = st.current_deviation(sch.stops[-1].plan_ts + 130)
    assert method == "fact" and dev == pytest.approx(120, abs=12)


def test_target_stop_window():
    sch, _ = line_schedule(n=30)
    st = VehicleState(1, 1, sch, CFG)
    k = st.target(T0)
    assert sch.stops[k].plan_ts - T0 == 660   # первая в (T+10, T+15] мин


def test_loop_route_does_not_match_far_future_visit():
    # та же точка остановки через 19 мин (кольцевой маршрут) не должна матчиться
    sch, dlon = line_schedule(n=3)
    far = PlannedStop(2000, T0 + 19 * 60, sch.stops[0].lon, 55.75, "k0", "Остановка 0 (обратно)")
    sch = VehicleSchedule(1, sch.stops + [far])
    st = VehicleState(1, 1, sch, CFG)
    st.ingest(T0 + 5, sch.stops[0].lon, 55.75, True, 0, 0, 0, False)
    assert 0 in st.arrivals and 3 not in st.arrivals


def test_time_jump_back_resets_state():
    sch, dlon = line_schedule()
    st = VehicleState(1, 1, sch, CFG)
    drive(st, sch, dlon, delay_s=0)
    assert st.arrivals
    st.ingest(T0 - 7200, 37.5, 55.75, True, 0, 0, 0, False)
    assert not any(a.detected for a in st.arrivals.values() if a.idx > 0)


def test_laplace_probability():
    assert laplace_sf(120, 120, 70) == pytest.approx(0.5)
    assert laplace_sf(120, 400, 70) > 0.97
    assert laplace_sf(120, -100, 70) < 0.03


@pytest.mark.parametrize("pred,expected", [(30, "green"), (150, "yellow"), (300, "red"), (-120, "yellow")])
def test_risk_levels(pred, expected):
    assert risk_level(pred, laplace_sf(CFG.late_threshold_s, pred, 70), CFG) == expected


def test_diagnose_standing_off_stop_is_primary():
    causes = diagnose(pred=300, cur_dev=100, dwell_s=240, at_stop=False, seg={}, speed=0, telemetry_age_s=5,
                      gps_fail_streak=0, trend=None, is_peak=True, source="model", cfg=CFG)
    assert causes[0].code == "STANDING_OFF_STOP"


def test_diagnose_no_signal():
    causes = diagnose(pred=200, cur_dev=150, dwell_s=0, at_stop=False, seg={}, speed=0, telemetry_age_s=600,
                      gps_fail_streak=0, trend=None, is_peak=False, source="model", cfg=CFG)
    assert causes[0].code == "NO_SIGNAL"


def test_clock_follows_stream_and_freeruns():
    c = DataClock()
    for i in range(60):                     # поток ×10: 1 с wall = 10 с данных
        c.observe(T0 + i * 10, wall=1000.0 + i)
    assert c.rate == pytest.approx(10, rel=0.05)
    assert c.now(wall=1059.0) == pytest.approx(T0 + 590)
    assert c.now(wall=1069.0) == pytest.approx(T0 + 690, rel=1e-6)   # обрыв: часы идут дальше
    c.observe(T0 - 10 * 3600, wall=1070.0)  # перезапуск реплея — пересинхронизация
    assert c.now(wall=1070.0) == pytest.approx(T0 - 10 * 3600)
