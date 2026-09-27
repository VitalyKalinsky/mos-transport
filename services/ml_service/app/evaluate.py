"""Оценка прогнозов модели по трём метрикам (ML-ядро не меняется, только вызывается).

1. score заказчика (dataset/README.md §5):
       mae_zero = mean(|факт|)
       score    = clip((mae_zero − MAE) / (mae_zero − MAE_TARGET), 0, 1)
   MAE_TARGET организаторы не публикуют. Если он не задан (--mae-target), он калибруется по
   опорной точке из README: baseline «прогноз = cur_dev_s» даёт score ≈ 0.40.
2. MAE, секунды.
3. F1 по классу «опоздание» (факт > 120 с, порог target_class=late): насколько точно по
   прогнозу поднимаются алерты диспетчеру (плюс precision / recall).

Запуск (в контейнере ML-сервиса, ./models и ./dataset смонтированы):

    docker compose run --rm --no-deps ml-service python -m app.evaluate
    docker compose run --rm --no-deps ml-service python -m app.evaluate --split train --shap
    docker compose run --rm --no-deps ml-service python -m app.evaluate --model models/finetuned/<время>.cbm
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import f1_score, precision_score, recall_score

import data_functions as dfn  # ML-ядро
from catboost import CatBoostRegressor

from .adapter import _align_keys
from .explain import global_importance, shap_values

LATE_THRESHOLD_S = 120.0     # target_class = late
BASELINE_SCORE = 0.40        # score baseline cur_dev_s по README датасета

SPLITS = {
    "test": ("dataset/labels/labels_test.csv", "dataset/test/traffic.csv"),
    "train": ("dataset/labels/labels_train.csv", "dataset/train/traffic.csv"),
}


def calibrate_mae_target(y_true, cur_dev, baseline_score: float = BASELINE_SCORE) -> float:
    """MAE_TARGET, при котором baseline «прогноз = cur_dev_s» получает baseline_score."""
    y, b = np.asarray(y_true, float), np.asarray(cur_dev, float)
    mae_zero, mae_base = np.mean(np.abs(y)), np.mean(np.abs(y - b))
    return float(mae_zero - (mae_zero - mae_base) / baseline_score)


def customer_score(y_true, y_pred, mae_target: float) -> tuple[float, float]:
    """(score ∈ [0, 1], score без обрезки) по формуле заказчика."""
    y, p = np.asarray(y_true, float), np.asarray(y_pred, float)
    mae_zero, mae = np.mean(np.abs(y)), np.mean(np.abs(y - p))
    raw = float((mae_zero - mae) / (mae_zero - mae_target))
    return float(np.clip(raw, 0.0, 1.0)), raw


def evaluate_predictions(y_true, y_pred, cur_dev=None, mae_target: float | None = None,
                         late_threshold_s: float = LATE_THRESHOLD_S) -> dict[str, float]:
    """Три метрики прогноза задержки: score заказчика, MAE и F1 алерта «опоздание».

    y_true, y_pred — фактическая и прогнозная задержка на целевой остановке, с.
    cur_dev — cur_dev_s тех же точек (нужен, только если mae_target не задан).
    """
    y, p = np.asarray(y_true, float), np.asarray(y_pred, float)
    if mae_target is None:
        if cur_dev is None:
            raise ValueError("нужен mae_target или cur_dev для его калибровки")
        mae_target = calibrate_mae_target(y, cur_dev)
    score, raw = customer_score(y, p, mae_target)
    late_true, late_pred = y > late_threshold_s, p > late_threshold_s
    return {
        "score": score,
        "score_raw": raw,
        "mae_s": float(np.mean(np.abs(y - p))),
        "f1_late": float(f1_score(late_true, late_pred, zero_division=0)),
        "precision_late": float(precision_score(late_true, late_pred, zero_division=0)),
        "recall_late": float(recall_score(late_true, late_pred, zero_division=0)),
        "mae_target_s": float(mae_target),
        "n": int(len(y)),
    }


def _path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else dfn.PROJECT_ROOT / p


def build_split(labels: str, traffic: str) -> pd.DataFrame:
    """Датасет признаков ровно как в ML-ядре (build_dataset), со статистиками расписания train."""
    sched = dfn.compute_schedule_features(dfn.load_and_preprocess_schedule(dfn.PATHS["train_schedule"]))
    lab = dfn.load_and_preprocess_labels(_path(labels))
    trf = dfn.load_and_preprocess_traffic(_path(traffic))
    return dfn.build_dataset(*_align_keys(lab, trf), sched)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.evaluate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=[*SPLITS, "both"], default="test")
    ap.add_argument("--labels", help="свои метки (вместо --split)")
    ap.add_argument("--traffic", help="телеметрия к своим меткам")
    ap.add_argument("--model", default=os.getenv("ML_MODEL_PATH", str(dfn.PATHS["model_save_path"])))
    ap.add_argument("--mae-target", type=float, help="MAE_TARGET заказчика (иначе — калибровка по baseline 0.40)")
    ap.add_argument("--shap", action="store_true", help="важность паттернов по SHAP и проверка аддитивности")
    a = ap.parse_args(argv)

    model = CatBoostRegressor()
    model.load_model(str(_path(a.model)))
    print(f"Модель: {a.model} ({model.tree_count_} деревьев)")

    if a.labels:
        splits = {"custom": (a.labels, a.traffic)}
    else:
        splits = SPLITS if a.split == "both" else {a.split: SPLITS[a.split]}

    for name, (labels, traffic) in splits.items():
        ds = build_split(labels, traffic)
        y, cur = ds["target_delay_s"].values, ds["cur_dev_s"].values
        mae_target = a.mae_target if a.mae_target is not None else calibrate_mae_target(y, cur)
        rows = {
            "модель": evaluate_predictions(y, dfn.predict(model, ds), mae_target=mae_target),
            "baseline cur_dev_s": evaluate_predictions(y, cur, mae_target=mae_target),
            "нулевой прогноз": evaluate_predictions(y, np.zeros_like(y, dtype=float), mae_target=mae_target),
        }
        src = "задан" if a.mae_target is not None else "калибровка: baseline = 0.40"
        print(f"\n=== {name}: {len(ds)} точек · MAE_TARGET = {mae_target:.1f} с ({src})")
        print(f"{'':20} {'score':>7} {'(raw)':>7} {'MAE, с':>8} {'F1 late':>8} {'prec':>6} {'recall':>6}")
        for k, m in rows.items():
            print(f"{k:20} {m['score']:7.3f} {m['score_raw']:7.3f} {m['mae_s']:8.2f} "
                  f"{m['f1_late']:8.3f} {m['precision_late']:6.3f} {m['recall_late']:6.3f}")
        if name == "test":
            print("  ! test использовался как eval_set при обучении (ранняя остановка) — оценка оптимистична")
        if name == "train":
            print("  ! train — обучающая выборка, оценка сильно оптимистична")

        if a.shap:
            contrib, base = shap_values(model, ds)
            recon = ds["cur_dev_s"].values + base + contrib.sum(axis=1)
            err = float(np.max(np.abs(recon - dfn.predict(model, ds))))
            print(f"\n  SHAP: cur_dev_s + base + Σвкладов = прогноз, макс. расхождение {err:.2e} с")
            print("  Средний |вклад| паттернов в прогноз, с:")
            for p in global_importance(model, ds):
                print(f"    {p['title']:36} {p['mean_abs_s']:7.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
