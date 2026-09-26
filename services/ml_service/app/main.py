"""ML-сервис: HTTP-обёртка над ML-ядром (инференс + переобучение).

Не содержит логики признаков/модели — всё делегируется в notebooks/data_functions.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .adapter import ModelRuntime

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ml.api")

runtime = ModelRuntime()
_latencies: deque[float] = deque(maxlen=2000)
_counters = {"requests": 0, "points": 0, "errors": 0}


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(runtime.load)
    yield


app = FastAPI(
    title="Mos-Transport ML Service",
    version="1.0.0",
    description="Инференс модели прогноза задержки (CatBoost, горизонт 10–15 мин). "
                "Признаки считаются функциями ML-ядра `notebooks/data_functions.py` без изменений.",
    lifespan=lifespan,
)


class PredictionPoint(BaseModel):
    sample_id: str = Field(examples=["131672_1767670500"])
    tr_id: int = Field(examples=[131672])
    T: str = Field(description="Момент прогноза (UTC, 'YYYY-MM-DD HH:MM:SS')", examples=["2026-01-06 03:35:00"])
    target_stop_id: int = Field(examples=[53700172828])
    target_time_begin: str = Field(description="Плановое прибытие на целевую остановку", examples=["2026-01-06 03:50:00"])
    cur_dev_s: float = Field(description="Отклонение на последней пройденной остановке, с", examples=[274.0])


class TelemetryRow(BaseModel):
    tr_id: int
    unit_id: int
    event_time: str
    location_valid: bool
    lon: float | None = None
    lat: float | None = None
    alt: float | None = None
    speed: float | None = None
    heading: float | None = None
    is_hist_data: bool = False


class PredictRequest(BaseModel):
    points: list[PredictionPoint]
    telemetry: list[TelemetryRow] = Field(
        default_factory=list,
        description="Сырая телеметрия (как строки traffic.csv) с event_time ≤ T; "
                    "достаточно последних ~20 пакетов на ТС",
    )


class Prediction(BaseModel):
    sample_id: str
    tr_id: int
    predicted_delay_s: float
    predicted_delta_s: float
    features: dict[str, Any]


class PredictResponse(BaseModel):
    predictions: list[Prediction]
    timings_ms: dict[str, float]
    model_validation_mae_s: float | None


@app.get("/health", tags=["service"])
def health():
    return {"status": "ok" if runtime.ready else "loading", "ready": runtime.ready}


@app.get("/v1/model", tags=["model"])
def model_info():
    return runtime.info()


@app.post("/v1/predict", response_model=PredictResponse, tags=["model"])
async def predict(req: PredictRequest):
    if not runtime.ready:
        raise HTTPException(503, "model is loading")
    if not req.points:
        return PredictResponse(predictions=[], timings_ms={"total_ms": 0.0},
                               model_validation_mae_s=runtime.validation_mae)
    t0 = time.perf_counter()
    try:
        preds, timings = await asyncio.to_thread(
            runtime.predict_batch,
            [p.model_dump() for p in req.points],
            [dict(r.model_dump(), packet_id=0, device_event_id=0, gps_time=r.event_time,
                  receive_time=r.event_time) for r in req.telemetry],
        )
    except Exception as e:  # noqa: BLE001
        _counters["errors"] += 1
        log.exception("predict failed")
        raise HTTPException(500, f"inference error: {e}") from e
    _counters["requests"] += 1
    _counters["points"] += len(preds)
    _latencies.append((time.perf_counter() - t0) * 1000)
    return PredictResponse(predictions=preds, timings_ms=timings,
                           model_validation_mae_s=runtime.validation_mae)


@app.post("/v1/train", tags=["model"])
def train(background: BackgroundTasks):
    """Переобучение по пайплайну ноутбука (функции ML-ядра). Выключено по умолчанию."""
    if os.getenv("ML_ENABLE_TRAIN", "false").lower() != "true":
        raise HTTPException(403, "training endpoint disabled (set ML_ENABLE_TRAIN=true)")
    if runtime.training_job.get("status") == "running":
        raise HTTPException(409, "training already running")
    background.add_task(runtime.retrain)
    return {"status": "started"}


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


@app.get("/v1/stats", tags=["service"])
def stats():
    lat = list(_latencies)
    return {
        **_counters,
        "latency_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "p99": _pct(lat, 0.99),
                       "max": max(lat) if lat else 0.0, "n": len(lat)},
    }


@app.get("/metrics", response_class=PlainTextResponse, tags=["service"])
def metrics():
    s = stats()
    lines = [
        f"ml_requests_total {s['requests']}",
        f"ml_points_total {s['points']}",
        f"ml_errors_total {s['errors']}",
    ]
    for q in ("p50", "p95", "p99"):
        lines.append(f'ml_request_latency_ms{{quantile="{q}"}} {s["latency_ms"][q]:.3f}')
    return "\n".join(lines) + "\n"
