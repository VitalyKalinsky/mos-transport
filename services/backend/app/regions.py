"""Регионы обслуживания: границы карты, центр, часовой пояс, картографические провайдеры.

Масштабирование на другой регион — добавить запись в data/regions/regions.json и задать REGION.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("backend.regions")

FALLBACK = {
    "id": "moscow", "name": "Москва и Московская область", "center": [55.7558, 37.6176], "zoom": 11,
    "min_zoom": 8, "max_zoom": 18, "bounds": [[54.20, 35.10], [56.99, 40.25]], "tz_offset_h": 3,
    "providers": ["osm"], "default_provider": "osm",
    "provider_defs": {"osm": {"name": "OpenStreetMap", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                              "subdomains": "", "crs": "EPSG3857", "max_zoom": 19,
                              "attribution": "&copy; участники OpenStreetMap"}},
}


def load_region(path: str, region_id: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        log.warning("regions file %s not found — fallback region", path)
        return FALLBACK
    rid = region_id or cfg.get("default")
    reg = cfg["regions"].get(rid)
    if reg is None:
        log.warning("region %s not in %s — using default", rid, path)
        rid = cfg["default"]
        reg = cfg["regions"][rid]
    out = dict(reg, id=rid)
    out["provider_defs"] = {k: cfg["providers"][k] for k in reg.get("providers", []) if k in cfg["providers"]}
    out["available_regions"] = [{"id": k, "name": v["name"]} for k, v in cfg["regions"].items()]
    return out
