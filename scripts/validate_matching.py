"""Офлайн-проверка онлайн-сопоставления телеметрии с расписанием.

Прогоняет test/traffic.csv через тот же код, что работает в Backend (app.tracker), в потоковом
режиме (по возрастанию event_time, пакет за пакетом), используя ТОЛЬКО плановое расписание.
Сравнивает:
  1) детектированное фактическое прибытие ↔ time_fact_begin из test/schedule.csv;
  2) онлайн-cur_dev_s на момент T ↔ cur_dev_s из labels_test.csv.

Запуск (из корня репозитория):
    python scripts/validate_matching.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "backend"))

from app.config import Settings  # noqa: E402
from app.reference import load_reference  # noqa: E402
from app.tracker import VehicleState  # noqa: E402


def main(split: str = "test") -> None:
    cfg = Settings()
    ref = load_reference(str(ROOT / f"dataset/{split}/schedule.csv"), str(ROOT / f"dataset/{split}/traffic.csv"))
    sch = pd.read_csv(ROOT / f"dataset/{split}/schedule.csv")
    fact = dict(zip(sch.tt_action_item_id, (pd.to_datetime(sch.time_fact_begin, format="ISO8601")
                                           - pd.Timestamp("1970-01-01")).dt.total_seconds()))
    labels = pd.read_csv(ROOT / f"dataset/labels/labels_{split}.csv")
    labels["T_ts"] = (pd.to_datetime(labels["T"]) - pd.Timestamp("1970-01-01")).dt.total_seconds()

    t = pd.read_csv(ROOT / f"dataset/{split}/traffic.csv", low_memory=False)
    t["ts"] = (pd.to_datetime(t.event_time, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t["rx"] = (pd.to_datetime(t.receive_time, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t = t[t.tr_id.isin(ref.schedules)].sort_values(["ts", "rx"])

    states = {tr: VehicleState(0, tr, ref.schedules[tr], cfg) for tr in ref.schedules}
    # события «метка T» вклиниваются в поток: считаем cur_dev_s в момент T
    lab_events = labels.sort_values("T_ts")[["sample_id", "tr_id", "T_ts", "cur_dev_s"]].to_records(index=False)
    li = 0
    online_cur = {}
    for r in t.itertuples(index=False):
        while li < len(lab_events) and lab_events[li].T_ts < r.ts:
            e = lab_events[li]
            online_cur[e.sample_id] = states[e.tr_id].current_deviation(float(e.T_ts))
            li += 1
        valid = bool(r.location_valid) and not pd.isna(r.lon)
        states[r.tr_id].ingest(r.ts, r.lon if valid else None, r.lat if valid else None, valid,
                               0.0 if pd.isna(r.speed) else r.speed, 0.0 if pd.isna(r.heading) else r.heading,
                               0.0 if pd.isna(r.alt) else r.alt, bool(r.is_hist_data))
    while li < len(lab_events):
        e = lab_events[li]
        online_cur[e.sample_id] = states[e.tr_id].current_deviation(float(e.T_ts))
        li += 1

    # 1) прибытия
    errs, total = [], 0
    for tr, st in states.items():
        stops = ref.schedules[tr].stops
        total += len(stops)
        for idx, a in st.arrivals.items():
            if a.detected:
                errs.append(a.fact_ts - fact[stops[idx].item_id])
    errs = np.array(errs)
    print(f"[arrivals] detected {len(errs)} / {total} planned stops ({len(errs) / total:.1%})")
    print(f"[arrivals] |detected − fact|: median {np.median(np.abs(errs)):.1f}s, "
          f"p75 {np.percentile(np.abs(errs), 75):.1f}s, p90 {np.percentile(np.abs(errs), 90):.1f}s, "
          f"within 60s: {(np.abs(errs) <= 60).mean():.1%}")

    # 2) cur_dev_s
    lab = labels.set_index("sample_id")
    diff = np.array([online_cur[s][0] - lab.loc[s, "cur_dev_s"] for s in lab.index])
    methods = pd.Series([online_cur[s][1] for s in lab.index]).value_counts().to_dict()
    print(f"[cur_dev_s] online vs label: MAE {np.mean(np.abs(diff)):.1f}s, median |err| "
          f"{np.median(np.abs(diff)):.1f}s, within 60s: {(np.abs(diff) <= 60).mean():.1%}  methods={methods}")
    by = pd.DataFrame({"m": [online_cur[s][1] for s in lab.index], "e": np.abs(diff)}).groupby("m")["e"]
    print("[cur_dev_s] MAE by method:", by.mean().round(1).to_dict(), "median:", by.median().round(1).to_dict())
    out = ROOT / "scripts" / "out"
    out.mkdir(exist_ok=True)
    pd.DataFrame({"sample_id": lab.index, "online_cur_dev_s": [online_cur[s][0] for s in lab.index],
                  "method": [online_cur[s][1] for s in lab.index]}).to_csv(out / f"online_cur_dev_{split}.csv", index=False)


if __name__ == "__main__":
    main(*(sys.argv[1:2] or ["test"]))
