"""Объяснение прогнозов через SHAP (встроенный в CatBoost TreeSHAP), без изменений ML-ядра.

Модель предсказывает дельту: прогноз = cur_dev_s + base + Σ SHAP(признак).
Вклады признаков (в секундах) группируются в паттерны поведения ТС, понятные диспетчеру.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

import data_functions as dfn  # ML-ядро (импортируется как есть)
from catboost import CatBoostRegressor, Pool

# паттерн → (название для диспетчера, признаки ML-ядра)
PATTERNS: dict[str, tuple[str, tuple[str, ...]]] = {
    "slow_pace": ("Низкий темп движения", (
        "speed", "speed_mean_5m", "speed_deficit", "required_speed_kmh", "speed_diff_from_hourly_mean")),
    "stop_and_go": ("Рваное движение (старт-стоп)", ("speed_diff", "speed_diff_std_5m")),
    "standing": ("Простой / долгая посадка", ("standing_ratio_5m",)),
    "current_deviation": ("Текущее отклонение от графика", ("cur_dev_s", "is_late_now")),
    "distance": ("Удалённость от целевой остановки", ("distance_to_target_m", "time_to_target_s")),
    "time_of_day": ("Время суток (пик / межпик)", ("hour", "minute_of_day", "sin_time", "cos_time")),
    "history": ("Исторический профиль рейса", (
        "sched_mean_delay", "sched_median_delay", "sched_std_delay", "sched_max_delay")),
    "vehicle": ("Особенности рейса (tr_id)", ("tr_id",)),
    "telemetry": ("Качество телеметрии (связь, GPS)", ("gps_failure", "telemetry_age_s")),
}
PATTERN_OF = {f: code for code, (_, feats) in PATTERNS.items() for f in feats}
assert set(PATTERN_OF) == set(dfn.FEATURE_COLS), "каждый признак ML-ядра должен входить в паттерн"


def shap_values(model: CatBoostRegressor, dataset: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """SHAP-вклады (n × признаки, секунды дельты) и базовое значение (n,)."""
    pool = Pool(dataset[dfn.FEATURE_COLS], cat_features=dfn.CAT_COLS)
    sv = model.get_feature_importance(pool, type="ShapValues")
    return sv[:, :-1], sv[:, -1]


def explain(model: CatBoostRegressor, dataset: pd.DataFrame, top_features: int = 5) -> list[dict[str, Any]]:
    """Объяснение каждого прогноза: cur_dev_s + base_s + Σ patterns[].seconds = predicted_delay_s."""
    if dataset.empty:
        return []
    contrib, base = shap_values(model, dataset)
    feats = list(dfn.FEATURE_COLS)
    out = []
    for i, row in enumerate(dataset[feats].to_dict("records")):
        by_pattern: dict[str, float] = {}
        for j, f in enumerate(feats):
            by_pattern[PATTERN_OF[f]] = by_pattern.get(PATTERN_OF[f], 0.0) + float(contrib[i, j])
        patterns = sorted(
            ({"code": c, "title": PATTERNS[c][0], "seconds": round(s, 1)} for c, s in by_pattern.items()),
            key=lambda p: -abs(p["seconds"]))
        order = np.argsort(-np.abs(contrib[i]))[:top_features]
        out.append({
            "cur_dev_s": round(float(dataset["cur_dev_s"].iloc[i]), 1),
            "base_s": round(float(base[i]), 1),
            "patterns": patterns,
            "top_features": [{"feature": feats[j], "value": _num(row[feats[j]]),
                              "seconds": round(float(contrib[i, j]), 1)} for j in order],
        })
    return out


def global_importance(model: CatBoostRegressor, dataset: pd.DataFrame) -> list[dict[str, Any]]:
    """Средний |вклад| паттернов по выборке (сек) — какие паттерны сильнее всего двигают прогноз."""
    contrib, _ = shap_values(model, dataset)
    per_pattern = pd.DataFrame(contrib, columns=dfn.FEATURE_COLS).T.groupby(PATTERN_OF).sum().T
    mean_abs = per_pattern.abs().mean().sort_values(ascending=False)
    return [{"code": c, "title": PATTERNS[c][0], "mean_abs_s": round(float(v), 2)} for c, v in mean_abs.items()]


def _num(v: Any) -> Any:
    try:
        f = float(v)
        return None if f != f else round(f, 3)
    except (TypeError, ValueError):
        return str(v)
