"""Построение геометрии маршрутов «по дорогам» для всех сегментов расписания.

Для каждого направленного сегмента «остановка A → остановка B»:
  1) берётся реальный GPS-трек ТС между детектированными прибытиями на A и B;
  2) трек привязывается к дорожному графу OSRM (/match);
  3) если трека нет или привязка неуспешна — маршрут по дорогам A → B (/route)
     через точки трека (если есть);
  4) последний fallback — сырой GPS-трек или прямая.
Результат кэшируется (скрипт можно прерывать и продолжать) и сохраняется в
data/network/shapes.json, который встраивается в образ Backend.

Запуск:  python scripts/build_route_shapes.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "backend"))

from app import routing  # noqa: E402
from app.config import Settings  # noqa: E402
from app.geo import haversine_m  # noqa: E402
from app.geometry import simplify  # noqa: E402
from app.reference import load_reference  # noqa: E402
from app.tracker import VehicleState  # noqa: E402

OUT = ROOT / "data" / "network" / "shapes.json"
CACHE = ROOT / "data" / "network" / ".shapes_cache.json"


def collect_tracks(ref) -> dict[str, list[list[tuple]]]:
    """directed seg_id -> список треков [(ts, lat, lon), ...] между прибытиями на A и B."""
    t = pd.read_csv(ROOT / "dataset/validate/traffic.csv", low_memory=False)
    t["ts"] = (pd.to_datetime(t.event_time, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t["rx"] = (pd.to_datetime(t.receive_time, format="ISO8601") - pd.Timestamp("1970-01-01")).dt.total_seconds()
    t = t[t.tr_id.isin(ref.schedules)].sort_values(["ts", "rx"])
    cfg = Settings()
    tracks: dict[str, list] = {}
    for tr, g in t.groupby("tr_id"):
        sch = ref.schedules[tr]
        st = VehicleState(0, tr, sch, cfg)
        pts: list[tuple] = []
        prev_arr = None
        for r in g.itertuples(index=False):
            valid = bool(r.location_valid) and not pd.isna(r.lon)
            arrs = st.ingest(r.ts, r.lon if valid else None, r.lat if valid else None, valid,
                             0.0 if pd.isna(r.speed) else r.speed, 0.0, 0.0, False)
            if valid:
                pts.append((r.ts, r.lat, r.lon))
            for a in arrs:
                if prev_arr is not None and a.idx == prev_arr.idx + 1:
                    A, B = sch.stops[prev_arr.idx], sch.stops[a.idx]
                    if A.stop_key != B.stop_key:
                        seg = [p for p in pts if prev_arr.fact_ts <= p[0] <= a.fact_ts]
                        tracks.setdefault(f"{A.stop_key}>{B.stop_key}", []).append(seg)
                prev_arr = a
                pts = [p for p in pts if p[0] >= a.fact_ts]
    return tracks


def build_one(A, B, obs: list[list[tuple]]) -> dict:
    straight = haversine_m(A.lon, A.lat, B.lon, B.lat)
    a, b = [A.lat, A.lon], [B.lat, B.lon]
    # лучший трек: больше точек, но без «разрывов» связи
    obs = sorted(obs, key=lambda s: -len(s))
    for seg in obs[:2]:
        if len(seg) < 2:
            continue
        pts = [a] + [[p[1], p[2]] for p in seg] + [b]
        ts = [int(seg[0][0]) - 1] + [int(p[0]) for p in seg] + [int(seg[-1][0]) + 1]
        ts = [max(ts[i], ts[i - 1]) if i else ts[i] for i in range(len(ts))]
        try:
            m = routing.match(pts[:100], ts[:100], [25] + [35] * (min(len(pts), 100) - 2) + [25])
            if m["confidence"] >= 0.3 and straight * 0.8 <= m["distance_m"] <= max(straight * 3, straight + 400):
                return {"coords": m["coords"], "source": "osrm_match", "length_m": m["distance_m"]}
        except routing.RoutingError:
            pass
        # маршрут по дорогам через точки трека (до 3 промежуточных)
        mid = [[p[1], p[2]] for p in seg[1:-1]]
        via = [mid[i * len(mid) // 4] for i in range(1, 4)] if len(mid) >= 3 else mid
        try:
            r = routing.route([a] + via + [b])
            if r["distance_m"] <= max(straight * 3, straight + 400):
                return {"coords": r["coords"], "source": "osrm_route_via_gps", "length_m": r["distance_m"]}
        except routing.RoutingError:
            pass
    try:
        r = routing.route([a, b])
        if r["distance_m"] <= max(straight * 4, straight + 800):
            return {"coords": r["coords"], "source": "osrm_route", "length_m": r["distance_m"]}
    except routing.RoutingError:
        pass
    if obs and len(obs[0]) >= 2:
        return {"coords": [a] + [[p[1], p[2]] for p in obs[0]] + [b], "source": "gps", "length_m": None}
    return {"coords": [a, b], "source": "straight", "length_m": straight}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    ref = load_reference(str(ROOT / "dataset/validate/schedule_plan.csv"), str(ROOT / "dataset/validate/traffic.csv"))
    tracks = collect_tracks(ref)
    # все направленные сегменты расписания (как в reference: без межрейсовых перегонов)
    segs: dict[str, tuple] = {}
    for sch in ref.schedules.values():
        for A, B in zip(sch.stops, sch.stops[1:]):
            if A.stop_key != B.stop_key and B.plan_ts - A.plan_ts <= 1800 \
                    and haversine_m(A.lon, A.lat, B.lon, B.lat) <= 3000:
                segs.setdefault(f"{A.stop_key}>{B.stop_key}", (A, B))
    cache = json.loads(CACHE.read_text("utf-8")) if CACHE.exists() else {}
    todo = [k for k in segs if k not in cache]
    if args.limit:
        todo = todo[:args.limit]
    print(f"segments: {len(segs)}, with GPS tracks: {sum(1 for k in segs if k in tracks)}, to build: {len(todo)}", flush=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    for n, k in enumerate(todo, 1):
        A, B = segs[k]
        res = build_one(A, B, tracks.get(k, []))
        res["coords"] = [[round(c[0], 6), round(c[1], 6)] for c in simplify(res["coords"], 2.0)]
        cache[k] = res
        if n % 20 == 0 or n == len(todo):
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
            src = pd.Series([v["source"] for v in cache.values()]).value_counts().to_dict()
            print(f"{n}/{len(todo)} {src}", flush=True)
    out = {k: v for k, v in cache.items() if k in segs}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), "utf-8")
    print(f"saved {len(out)} shapes -> {OUT}")


if __name__ == "__main__":
    main()
