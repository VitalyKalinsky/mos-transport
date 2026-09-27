"""Дообучение модели из консоли (без HTTP): поверх деревьев текущей модели достраиваются новые.

Бустинг продолжается через init_model, как в fine_tune ML-ядра (само ядро не меняется).
Запуск в отдельном контейнере (живой сервис не тормозит; ./models и ./dataset смонтированы):

    docker compose run --rm --no-deps ml-service python -m app.finetune
    docker compose run --rm --no-deps ml-service python -m app.finetune --activate
    docker compose restart ml-service        # сервис подхватывает активированную модель

Новые данные — CSV в формате датасета (labels_*.csv + traffic.csv), пути относительно корня
проекта: --labels dataset/new/labels.csv --traffic dataset/new/traffic.csv.
По умолчанию берётся день test. Без --val-labels валидация — последние --val-ratio новых
данных по времени. Модель активируется, только если MAE на валидации лучше текущей (или --force).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import data_functions as dfn  # ML-ядро
from catboost import CatBoostRegressor, Pool

from .adapter import _align_keys

ROOT = dfn.PROJECT_ROOT


def _path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def _dataset(labels: Path, traffic: Path, sched_feats: pd.DataFrame) -> pd.DataFrame:
    lab = dfn.load_and_preprocess_labels(labels)
    trf = dfn.load_and_preprocess_traffic(traffic)
    return dfn.build_dataset(*_align_keys(lab, trf), sched_feats)


def _mae(model: CatBoostRegressor, df: pd.DataFrame) -> float:
    return float(np.mean(np.abs(dfn.predict(model, df) - df["target_delay_s"].values)))


def fine_tune(base: CatBoostRegressor, train_df: pd.DataFrame, val_df: pd.DataFrame,
              iterations: int, lr: float, train_dir: Path) -> CatBoostRegressor:
    """То же, что dfn.fine_tune (init_model, параметры ядра), но добавляет ровно `iterations`
    деревьев: dfn.fine_tune передаёт base_trees + additional, а CatBoost при init_model
    считает iterations числом НОВЫХ деревьев. ML-ядро не меняем — обходим здесь."""
    def pool(df: pd.DataFrame) -> Pool:
        return Pool(df[dfn.FEATURE_COLS], label=df["target_delay_s"] - df["cur_dev_s"], cat_features=dfn.CAT_COLS)
    train_dir.mkdir(parents=True, exist_ok=True)
    model = CatBoostRegressor(iterations=iterations, learning_rate=lr, depth=6, loss_function="MAE",
                              eval_metric="MAE", random_seed=42, verbose=25, train_dir=str(train_dir))
    model.fit(pool(train_df.sort_values("T")), eval_set=pool(val_df), init_model=base,
              early_stopping_rounds=30, use_best_model=True)
    print(f"\nБыло деревьев: {base.tree_count_}, добавлено: {model.tree_count_ - base.tree_count_}")
    return model


def main(argv: list[str] | None = None) -> int:
    active = Path(os.getenv("ML_MODEL_PATH", str(dfn.PATHS["model_save_path"])))
    ap = argparse.ArgumentParser(prog="python -m app.finetune", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", default="dataset/labels/labels_test.csv", help="метки новых данных")
    ap.add_argument("--traffic", default="dataset/test/traffic.csv", help="телеметрия новых данных")
    ap.add_argument("--val-labels", help="метки валидации (иначе — хвост новых данных)")
    ap.add_argument("--val-traffic", help="телеметрия валидации")
    ap.add_argument("--val-ratio", type=float, default=0.2, help="доля хвоста новых данных под валидацию")
    ap.add_argument("--base", default=str(active), help="исходная модель (по умолчанию — активная)")
    ap.add_argument("--iterations", type=int, default=150, help="сколько деревьев добавить (максимум)")
    ap.add_argument("--lr", type=float, default=0.025, help="learning rate дообучения")
    ap.add_argument("--output", help="куда сохранить (по умолчанию models/finetuned/<время>.cbm)")
    ap.add_argument("--activate", action="store_true", help="заменить активную модель, если MAE лучше")
    ap.add_argument("--force", action="store_true", help="с --activate: заменить даже без улучшения")
    a = ap.parse_args(argv)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = dfn.MODELS_DIR / "finetuned" / stamp
    out = _path(a.output) if a.output else run_dir.with_suffix(".cbm")
    sched = dfn.compute_schedule_features(dfn.load_and_preprocess_schedule(dfn.PATHS["train_schedule"]))
    print(f"Новые данные: {a.labels} + {a.traffic}")
    new = _dataset(_path(a.labels), _path(a.traffic), sched).sort_values("T").reset_index(drop=True)
    if a.val_labels:
        val = _dataset(_path(a.val_labels), _path(a.val_traffic or a.traffic), sched)
        train_part = new
    else:
        split = int(len(new) * (1 - a.val_ratio))
        train_part, val = new.iloc[:split], new.iloc[split:]
    if train_part.empty or val.empty:
        print("Недостаточно данных для дообучения и валидации", file=sys.stderr)
        return 1
    print(f"Обучение: {len(train_part)} точек, валидация: {len(val)} точек")

    base = CatBoostRegressor()
    base.load_model(str(_path(a.base)))
    mae_base = _mae(base, val)
    mae_naive = float(np.mean(np.abs(val["cur_dev_s"] - val["target_delay_s"])))

    # логи CatBoost этого запуска — отдельно, чтобы не затирать логи активной модели
    model = fine_tune(base, train_part, val, a.iterations, a.lr, train_dir=run_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out))
    mae_new = _mae(model, val)

    print("\n--- Валидация, MAE, с ---")
    print(f"baseline cur_dev_s: {mae_naive:8.2f}")
    print(f"исходная модель:    {mae_base:8.2f}  ({base.tree_count_} деревьев)")
    print(f"дообученная:        {mae_new:8.2f}  ({model.tree_count_} деревьев)")
    meta = {"validation_mae_s": round(mae_new, 3), "base_mae_s": round(mae_base, 3),
            "base_model": str(_path(a.base)), "labels": a.labels, "traffic": a.traffic,
            "trees": model.tree_count_, "created": stamp}
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    if not a.activate:
        print(f"\nМодель сохранена: {out}\nАктивировать: добавьте --activate")
        return 0
    if mae_new >= mae_base and not a.force:
        print("\nMAE не улучшилась — активная модель не заменена (заменить всё равно: --force)")
        return 2
    backup = dfn.MODELS_DIR / "backup" / f"{active.stem}.{stamp}.cbm"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if active.exists():
        shutil.copy2(active, backup)
        if active.with_suffix(".json").exists():
            shutil.copy2(active.with_suffix(".json"), backup.with_suffix(".json"))
    shutil.copy2(out, active)
    shutil.copy2(out.with_suffix(".json"), active.with_suffix(".json"))
    print(f"\nАктивная модель заменена: {active}\nПредыдущая сохранена: {backup}"
          "\nПрименить: docker compose restart ml-service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
