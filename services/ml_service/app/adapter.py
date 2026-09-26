"""Тонкий адаптер вокруг ML-ядра (notebooks/data_functions.py + models/catboost_model.cbm).

ML-код НЕ дублируется и НЕ модифицируется: онлайн-запрос сериализуется в тот же
табличный вид, что и CSV датасета, и прогоняется через те же функции
load_and_preprocess_labels / load_and_preprocess_traffic / build_dataset / predict,
что использовались при обучении. Это гарантирует идентичность признаков
train ↔ serve (нет training/serving skew).
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pandas as pd

import data_functions as dfn  # ML-ядро (импортируется как есть)
from catboost import CatBoostRegressor

log = logging.getLogger("ml.adapter")

POINT_COLUMNS = ["sample_id", "tr_id", "T", "target_stop_id", "target_time_begin", "cur_dev_s"]
TRAFFIC_COLUMNS = [
    "packet_id", "tr_id", "unit_id", "event_time", "device_event_id", "location_valid",
    "gps_time", "lon", "lat", "alt", "speed", "heading", "receive_time", "is_hist_data",
]


def _to_buffer(df: pd.DataFrame) -> io.StringIO:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return buf


class ModelRuntime:
    """Держит загруженную модель и исторические признаки расписания; потокобезопасная замена модели."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.model: CatBoostRegressor | None = None
        self.schedule_features: pd.DataFrame | None = None
        self.model_path = Path(os.getenv("ML_MODEL_PATH", str(dfn.PATHS["model_save_path"])))
        self.schedule_path = Path(os.getenv("ML_HIST_SCHEDULE_PATH", str(dfn.PATHS["train_schedule"])))
        self.loaded_at: float | None = None
        self.validation_mae: float | None = None
        self.training_job: dict[str, Any] = {"status": "idle"}

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        t0 = time.perf_counter()
        model = CatBoostRegressor()
        model.load_model(str(self.model_path))
        # Исторические статистики опозданий по ТС — ровно как в ноутбуке data_work.ipynb
        sched = dfn.load_and_preprocess_schedule(self.schedule_path)
        feats = dfn.compute_schedule_features(sched)
        with self._lock:
            self.model = model
            self.schedule_features = feats
            self.loaded_at = time.time()
            self.validation_mae = self._read_validation_mae()
        log.info("model loaded from %s in %.2fs (hist stats for %d vehicles)",
                 self.model_path, time.perf_counter() - t0, len(feats))
        self._warmup()

    def _read_validation_mae(self) -> float | None:
        tsv = Path(dfn.PATHS["catboost_info"]) / "test_error.tsv"
        try:
            return float(pd.read_csv(tsv, sep="\t")["MAE"].min())
        except Exception:  # noqa: BLE001 — метрика опциональна
            return None

    def _warmup(self) -> None:
        """Прогрев (JIT/аллокации CatBoost) — чтобы первый реальный запрос не был медленным."""
        try:
            self.predict_batch(
                [{"sample_id": "warmup", "tr_id": 0, "T": "2026-01-06 08:00:00",
                  "target_stop_id": 0, "target_time_begin": "2026-01-06 08:12:00", "cur_dev_s": 0.0}],
                [],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("warmup failed: %s", e)

    @property
    def ready(self) -> bool:
        return self.model is not None

    # --------------------------------------------------------------- predict
    def predict_batch(self, points: list[dict], telemetry: list[dict]) -> tuple[list[dict], dict[str, float]]:
        timings: dict[str, float] = {}
        t0 = time.perf_counter()
        with self._lock:
            model, sched_feats = self.model, self.schedule_features
        if model is None:
            raise RuntimeError("model is not loaded")

        pts = pd.DataFrame(points, columns=POINT_COLUMNS)
        for c in ("T", "target_time_begin"):
            pts[c] = _norm_time(pts[c])
        labels = dfn.load_and_preprocess_labels(_to_buffer(pts))

        tel = pd.DataFrame(telemetry, columns=TRAFFIC_COLUMNS)
        if tel.empty:
            # нет телеметрии → build_dataset сам проставит telemetry_missing=1 и дефолты
            traffic = pd.DataFrame({
                "tr_id": pd.Series(dtype="int64"),
                "event_time": pd.Series(dtype=labels["T"].dtype),
                "speed": pd.Series(dtype="float64"),
                "is_standing": pd.Series(dtype="int64"),
                "speed_diff": pd.Series(dtype="float64"),
                "heading": pd.Series(dtype="float64"),
                "alt": pd.Series(dtype="float64"),
                "gps_failure": pd.Series(dtype="int64"),
            })
        else:
            tel["location_valid"] = tel["location_valid"].astype(bool)
            tel["device_event_id"] = tel["device_event_id"].fillna(0)
            tel["event_time"] = _norm_time(tel["event_time"])
            traffic = dfn.load_and_preprocess_traffic(_to_buffer(tel))
        timings["preprocess_ms"] = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        # dtype-выравнивание ключей merge_asof (tr_id int64 с обеих сторон)
        labels["tr_id"] = labels["tr_id"].astype("int64")
        if not traffic.empty:
            traffic["tr_id"] = traffic["tr_id"].astype("int64")
        dataset = dfn.build_dataset(labels, traffic, sched_feats)
        timings["features_ms"] = (time.perf_counter() - t1) * 1000

        t2 = time.perf_counter()
        preds = dfn.predict(model, dataset)
        timings["inference_ms"] = (time.perf_counter() - t2) * 1000
        timings["total_ms"] = (time.perf_counter() - t0) * 1000

        out = []
        feat_cols = list(dfn.FEATURE_COLS)
        for row, pred in zip(dataset.to_dict("records"), preds):
            out.append({
                "sample_id": str(row["sample_id"]),
                "tr_id": int(row["tr_id"]),
                "predicted_delay_s": float(pred),
                "predicted_delta_s": float(pred - row["cur_dev_s"]),
                "features": {c: _jsonable(row.get(c)) for c in feat_cols},
            })
        return out, timings

    # ----------------------------------------------------------------- info
    def info(self) -> dict[str, Any]:
        with self._lock:
            model = self.model
        importances = {}
        if model is not None:
            try:
                importances = dict(zip(dfn.FEATURE_COLS, map(float, model.get_feature_importance())))
            except Exception:  # noqa: BLE001
                importances = {}
        return {
            "ready": model is not None,
            "model_path": str(self.model_path),
            "model_type": type(model).__name__ if model is not None else None,
            "tree_count": model.tree_count_ if model is not None else None,
            "feature_cols": list(dfn.FEATURE_COLS),
            "cat_cols": list(dfn.CAT_COLS),
            "target": "target_delay_s = cur_dev_s + predicted_delta",
            "validation_mae_s": self.validation_mae,
            "feature_importance": importances,
            "hist_schedule_vehicles": 0 if self.schedule_features is None else len(self.schedule_features),
            "loaded_at": self.loaded_at,
            "training_job": self.training_job,
        }

    # ----------------------------------------------------------------- train
    def retrain(self) -> None:
        """Переобучение ровно по пайплайну ноутбука data_work.ipynb (функции ML-ядра без изменений).

        Модель сохраняется внутри контейнера (путь из data_functions.PATHS) и подменяется «на горячую».
        """
        self.training_job = {"status": "running", "started_at": time.time()}
        try:
            P = dfn.PATHS
            train_traffic = dfn.load_and_preprocess_traffic(P["train_traffic"])
            train_labels = dfn.load_and_preprocess_labels(P["train_labels"])
            train_schedule = dfn.load_and_preprocess_schedule(P["train_schedule"])
            schedule_features = dfn.compute_schedule_features(train_schedule)
            train_full = dfn.build_dataset(train_labels, train_traffic, schedule_features)
            test_traffic = dfn.load_and_preprocess_traffic(P["test_traffic"])
            test_labels = dfn.load_and_preprocess_labels(P["test_labels"])
            test_full = dfn.build_dataset(test_labels, test_traffic, schedule_features)
            model = dfn.train(train_df=train_full, val_df=test_full)
            with self._lock:
                self.model = model
                self.schedule_features = schedule_features
                self.loaded_at = time.time()
                self.validation_mae = self._read_validation_mae()
            self.training_job = {"status": "done", "finished_at": time.time(),
                                 "validation_mae_s": self.validation_mae}
        except Exception as e:  # noqa: BLE001
            log.exception("training failed")
            self.training_job = {"status": "failed", "error": str(e), "finished_at": time.time()}


def _norm_time(s: pd.Series) -> pd.Series:
    """Единый строковый формат времени (как в CSV датасета), иначе to_datetime внутри
    ML-ядра падает на смеси 'HH:MM:SS' и 'HH:MM:SS.ffffff'."""
    return pd.to_datetime(s, format="ISO8601").dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return str(v)
