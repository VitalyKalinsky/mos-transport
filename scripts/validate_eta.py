"""Точность ETA на ближайшие остановки (офлайн-проверка на фактах test/schedule.csv).

Поток test/traffic.csv прогоняется через трекер Backend. Каждые 5 минут данных для каждого ТС:
  * live      — ETA на следующие 8 остановок по текущему состоянию;
  * offline G — связь потеряна: состояние «замораживается» в T0 и ETA строятся в T0+G
                по последнему известному положению/темпу (dead reckoning), G = 2, 5, 10 мин.
Сравнение с фактом прибытия (time_fact_begin). Бейзлайны: план + текущее отклонение; только план.

Запуск: python scripts/validate_eta.py
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "backend"))

from app.config import Settings  # noqa: E402
from app.eta import EtaEngine  # noqa: E402
from app.reference import load_reference  # noqa: E402
from app.tracker import VehicleState  # noqa: E402

GAPS = (120, 300, 600)
K = 8


def main(split: str = "test") -> None:
    cfg = Settings()
    ref = load_reference(str(ROOT / f"dataset/{split}/schedule.csv"), str(ROOT / f"dataset/{split}/traffic.csv"),
                         str(ROOT / "data/network/shapes.json"))
    eng = EtaEngine(ref)
    sch = pd.read_csv(ROOT / f"dataset/{split}/schedule.csv")
    fact = dict(zip(sch.tt_action_item_id, (pd.to_datetime(sch.time_fact_begin, format="ISO8601")
                                           - pd.Timestamp("1970-01-01")).dt.total_seconds()))
    t = pd.read_csv(ROOT / f"dataset/{split}/traffic.csv", low_memory=False)
    t["ts"] = (pd.to_datetime(t.event_time, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t["rx"] = (pd.to_datetime(t.receive_time, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t = t[t.tr_id.isin(ref.schedules)].sort_values(["ts", "rx"])

    states = {tr: VehicleState(0, tr, ref.schedules[tr], cfg) for tr in ref.schedules}
    rows = []
    frozen: list[tuple[float, float, VehicleState]] = []   # (T0, evaluate_at, snapshot)
    grid = float(t.ts.min() // 300 * 300 + 300)

    def evaluate(st: VehicleState, T: float, mode: str) -> None:
        if st.schedule is None or not st.initialized or st.lat is None or T - st.last_ts > 900 and mode == "live":
            return
        res = eng.predict(st, T, K)
        cur, _ = st.current_deviation(T)
        for h, e in enumerate(res.stops, 1):
            f = fact.get(e.stop_id)
            if f is None or f < T or e.plan_ts - T > 3600:
                continue
            rows.append({"mode": mode, "h": h, "ahead_min": (f - T) / 60, "err": e.eta_ts - f,
                         "err_plan_dev": e.plan_ts + cur - f, "err_plan": e.plan_ts - f,
                         "est": res.mode})

    for r in t.itertuples(index=False):
        while r.ts >= grid:
            for st in states.values():
                evaluate(st, grid, "live")
                for g in GAPS:
                    frozen.append((grid, grid + g, copy.deepcopy(st)))
            still = []
            for T0, at, snap in frozen:
                if at <= grid:
                    evaluate(snap, at, f"offline_{int((at - T0) // 60)}min")
                else:
                    still.append((T0, at, snap))
            frozen = still
            grid += 300
        valid = bool(r.location_valid) and not pd.isna(r.lon)
        states[r.tr_id].ingest(r.ts, r.lon if valid else None, r.lat if valid else None, valid,
                               0.0 if pd.isna(r.speed) else r.speed, 0.0, 0.0, False)

    d = pd.DataFrame(rows)
    d["abs"], d["abs_pd"], d["abs_p"] = d.err.abs(), d.err_plan_dev.abs(), d.err_plan.abs()
    bins = [0, 2, 5, 10, 15, 30, 60]
    d["bucket"] = pd.cut(d.ahead_min, bins)
    print(f"samples: {len(d)}")
    for mode, g in d.groupby("mode"):
        print(f"\n== {mode}: MAE ETA {g['abs'].mean():.1f}s | план+текущее откл. {g.abs_pd.mean():.1f}s | "
              f"только план {g.abs_p.mean():.1f}s (n={len(g)})")
        tab = g.groupby("bucket", observed=True).agg(n=("abs", "size"), eta=("abs", "mean"), eta_med=("abs", "median"),
                                                    plan_dev=("abs_pd", "mean"), plan=("abs_p", "mean")).round(1)
        print(tab.to_string())
    out = ROOT / "scripts" / "out"
    out.mkdir(exist_ok=True)
    d.drop(columns=["bucket"]).to_csv(out / f"eta_eval_{split}.csv", index=False)


if __name__ == "__main__":
    main(*(sys.argv[1:2] or ["test"]))
