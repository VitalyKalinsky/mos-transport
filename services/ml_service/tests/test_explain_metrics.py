"""Тесты SHAP-объяснений и метрик (запуск — в контейнере ML-сервиса, см. README)."""
import os

import numpy as np
import pytest

import data_functions as dfn
from app.evaluate import build_split, calibrate_mae_target, customer_score, evaluate_predictions
from app.explain import PATTERNS, explain, shap_values
from catboost import CatBoostRegressor


# ------------------------------------------------------------------ метрики
def test_baseline_calibrated_to_040():
    rng = np.random.default_rng(0)
    y = rng.normal(60, 120, 500)
    cur = y + rng.normal(0, 80, 500)
    m = evaluate_predictions(y, cur, cur_dev=cur)
    assert m["score"] == pytest.approx(0.40)


def test_score_bounds_and_mae():
    y = np.array([100.0, -50.0, 300.0, 0.0])
    assert customer_score(y, y, mae_target=40)[0] == 1.0          # идеальный прогноз
    assert customer_score(y, np.zeros(4), mae_target=40)[0] == 0.0  # нулевой прогноз
    m = evaluate_predictions(y, y + 10, mae_target=40)
    assert m["mae_s"] == pytest.approx(10.0)


def test_f1_late():
    y = np.array([200.0, 150.0, 0.0, 30.0])       # 2 опоздания > 120 с
    p = np.array([210.0, 50.0, 130.0, 0.0])       # TP=1, FN=1, FP=1
    m = evaluate_predictions(y, p, mae_target=0)
    assert (m["precision_late"], m["recall_late"], m["f1_late"]) == pytest.approx((0.5, 0.5, 0.5))


def test_calibration_formula():
    y, cur = np.array([100.0, 200.0]), np.array([80.0, 150.0])
    t = calibrate_mae_target(y, cur)
    assert customer_score(y, cur, t)[1] == pytest.approx(0.40)


# ------------------------------------------------------------------ SHAP на реальной модели
@pytest.fixture(scope="module")
def model_and_data():
    model = CatBoostRegressor()
    model.load_model(os.getenv("ML_MODEL_PATH", str(dfn.PATHS["model_save_path"])))
    ds = build_split("dataset/labels/labels_test.csv", "dataset/test/traffic.csv").head(40)
    return model, ds


def test_shap_additivity(model_and_data):
    model, ds = model_and_data
    contrib, base = shap_values(model, ds)
    recon = ds["cur_dev_s"].values + base + contrib.sum(axis=1)
    np.testing.assert_allclose(recon, dfn.predict(model, ds), atol=1e-3)


def test_explanation_structure(model_and_data):
    model, ds = model_and_data
    ex = explain(model, ds)
    preds = dfn.predict(model, ds)
    assert len(ex) == len(ds)
    for e, p in zip(ex, preds):
        assert {x["code"] for x in e["patterns"]} == set(PATTERNS)
        total = e["cur_dev_s"] + e["base_s"] + sum(x["seconds"] for x in e["patterns"])
        assert total == pytest.approx(p, abs=1.0)   # округление до 0.1 с
        assert len(e["top_features"]) == 5
