"""Честная оценка качества ОНЛАЙН-контура на labels_test.csv через работающий ML-сервис.

Сравнивает MAE прогноза при:
  A) cur_dev_s из разметки (как в офлайн-обучении),
  B) cur_dev_s, посчитанном онлайн Backend-логикой по GPS (scripts/validate_matching.py),
  C) baseline: прогноз = cur_dev_s (из разметки) — «пол» из README датасета.

Также меряет latency ML-сервиса на батчах разного размера.

Запуск (сервисы подняты через docker compose):
    python scripts/validate_matching.py     # создаёт scripts/out/online_cur_dev_test.csv
    python scripts/evaluate_online.py [http://localhost:8001]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
ML = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(ML + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> None:
    labels = pd.read_csv(ROOT / "dataset/labels/labels_test.csv")
    online = pd.read_csv(ROOT / "scripts/out/online_cur_dev_test.csv").set_index("sample_id")
    t = pd.read_csv(ROOT / "dataset/test/traffic.csv", low_memory=False)
    t["et"] = pd.to_datetime(t.event_time, format="ISO8601")
    t["rt"] = pd.to_datetime(t.receive_time, format="ISO8601")
    t = t.sort_values(["et", "rt"])
    by_tr = {tr: g for tr, g in t.groupby("tr_id")}

    def telemetry(tr, T):
        g = by_tr.get(tr)
        if g is None:
            return []
        w = g[g.et <= pd.Timestamp(T)].tail(20)
        return [{"tr_id": int(r.tr_id), "unit_id": int(r.unit_id), "event_time": str(r.event_time),
                 "location_valid": bool(r.location_valid),
                 **{k: (None if pd.isna(getattr(r, k)) else float(getattr(r, k))) for k in ("lon", "lat", "alt", "speed", "heading")},
                 "is_hist_data": bool(r.is_hist_data)} for r in w.itertuples()]

    res = {"label": [], "online": []}
    tel_cache = {}
    for mode in ("label", "online"):
        pts, tel = [], []
        for r in labels.itertuples():
            cur = r.cur_dev_s if mode == "label" else float(online.loc[r.sample_id, "online_cur_dev_s"])
            pts.append({"sample_id": r.sample_id, "tr_id": int(r.tr_id), "T": r.T, "target_stop_id": int(r.target_stop_id),
                        "target_time_begin": r.target_time_begin, "cur_dev_s": cur})
            key = (r.tr_id, r.T)
            if key not in tel_cache:
                tel_cache[key] = telemetry(r.tr_id, r.T)
            tel.extend(tel_cache[key])
        # по одной точке на запрос — как в онлайне на каждый ТС (телеметрия своя у каждой точки)
        preds = {}
        for p in pts:
            out = post("/v1/predict", {"points": [p], "telemetry": tel_cache[(p["tr_id"], p["T"])]})
            preds[p["sample_id"]] = out["predictions"][0]["predicted_delay_s"]
        res[mode] = np.array([preds[s] for s in labels.sample_id])

    y = labels.target_delay_s.values
    mae = lambda p: float(np.mean(np.abs(y - p)))  # noqa: E731
    mae_zero = float(np.mean(np.abs(y)))
    print(f"n = {len(y)}")
    print(f"MAE zero-forecast           : {mae_zero:.1f} s")
    print(f"MAE baseline cur_dev (label): {mae(labels.cur_dev_s.values):.1f} s")
    print(f"MAE baseline cur_dev (online): {mae(online.loc[labels.sample_id, 'online_cur_dev_s'].values):.1f} s")
    print(f"MAE model, cur_dev из разметки : {mae(res['label']):.1f} s")
    print(f"MAE model, cur_dev онлайн (GPS): {mae(res['online']):.1f} s")

    # latency на батчах
    r0 = labels.iloc[0]
    base = {"sample_id": "x", "tr_id": int(r0["tr_id"]), "T": r0["T"], "target_stop_id": 1,
            "target_time_begin": r0["target_time_begin"], "cur_dev_s": 0.0}
    tel0 = tel_cache[(r0["tr_id"], r0["T"])]
    for n in (1, 10, 50, 200, 1000):
        lat = []
        for _ in range(5):
            body = {"points": [dict(base, sample_id=str(i)) for i in range(n)], "telemetry": tel0 * max(1, n // 5)}
            t0 = time.perf_counter()
            post("/v1/predict", body)
            lat.append((time.perf_counter() - t0) * 1000)
        print(f"ML batch {n:5d} points: median {np.median(lat):7.1f} ms  ({np.median(lat) / n:.2f} ms/point)")


if __name__ == "__main__":
    main()
