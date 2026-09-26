"""Автопривязка ТС датасета к реальным маршрутам OpenStreetMap (номер маршрута, остановки, геометрия).

Для каждого ТС: по 6 остановкам его графика ищутся маршруты OSM, проходящие по дорогам рядом
(way(around) → rel(bw)); кандидаты оцениваются долей ВСЕХ остановок ТС в пределах 40 м от линии маршрута.
Лучший вариант (оба направления с тем же номером) сохраняется в data/routes/ с привязкой ТС.

Запуск: python scripts/link_osm_routes.py [--min-score 0.6]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "backend"))

from app.reference import load_reference  # noqa: E402
from app.routes_registry import RouteRegistry, import_osm, overpass, score_vehicle_route  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=float, default=0.6)
    args = ap.parse_args()
    ref = load_reference(str(ROOT / "dataset/validate/schedule_plan.csv"), str(ROOT / "dataset/validate/traffic.csv"),
                         str(ROOT / "data/network/shapes.json"))
    reg = RouteRegistry(str(ROOT / "data/routes"), str(ROOT / "data/routes/_none"))
    report = {}
    linked = reg.vehicle_links()
    for tr, sch in ref.schedules.items():
        if tr in linked:                       # повторный запуск дорабатывает только непривязанные ТС
            report[tr] = {"ref": linked[tr]["ref"], "score": None, "cached": True}
            continue
        uniq = list({s.stop_key: (s.lat, s.lon) for s in sch.stops}.values())
        sample = [uniq[i * len(uniq) // 6] for i in range(6)]
        q = "[out:json][timeout:60];(" + "".join(
            f'way(around:25,{lat},{lon})["highway"];' for lat, lon in sample) + ")->.w;" \
            'rel(bw.w)["type"="route"]["route"~"^(bus|trolleybus|tram)$"];out tags;'
        try:
            rels = overpass(q)["elements"]
        except Exception as e:  # noqa: BLE001
            print(f"{tr}: overpass error {e}")
            continue
        refs = Counter(r["tags"].get("ref") for r in rels if r.get("tags", {}).get("ref"))
        best = None
        for rref, _ in refs.most_common(6):
            try:
                variants = import_osm(rref, None, [[54.2, 35.1], [56.99, 40.25]])
            except Exception as e:  # noqa: BLE001
                print(f"  {tr} {rref}: {e}")
                continue
            # доля остановок ТС, лежащих на любом из направлений маршрута
            score = sum(1 for lat, lon in uniq if any(score_vehicle_route([(lat, lon)], v) for v in variants)) / len(uniq)
            print(f"  {tr}: candidate {rref} score {score:.2f}")
            if best is None or score > best[1]:
                best = (rref, score, variants)
        if best and best[1] >= args.min_score:
            # сохраняем только направления, реально проходящие по остановкам ТС
            keep = [v for v in best[2] if score_vehicle_route(uniq, v) >= 0.3] or best[2][:2]
            for v in keep:
                v["vehicles"] = sorted(set(v.get("vehicles", [])) | {tr})
                v["link_score"] = round(best[1], 3)
                reg.save(v)
            report[tr] = {"ref": best[0], "score": round(best[1], 3)}
            print(f"{tr}: -> {best[0]} ({best[1]:.0%})")
        else:
            report[tr] = {"ref": None, "score": round(best[1], 3) if best else 0}
            print(f"{tr}: no confident match ({report[tr]})")
    (ROOT / "scripts" / "out").mkdir(exist_ok=True)
    (ROOT / "scripts" / "out" / "osm_links.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), "utf-8")


if __name__ == "__main__":
    main()
