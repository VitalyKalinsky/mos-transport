"""Backend: приём NDTP, сопоставление с расписанием, оркестрация прогнозов, API и WebSocket для дашборда."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import ndtp
from fastapi import Body, FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from .config import settings
from .engine import Engine
from .ml_client import MLClient
from .ndtp_server import NDTPServer
from .reference import load_reference

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("backend")

STARTED = time.time()
state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    t0 = time.perf_counter()
    ref = await asyncio.to_thread(load_reference, settings.schedule_path, settings.units_path)
    ml = MLClient(settings)
    engine = Engine(settings, ref, ml)
    server = NDTPServer(settings.ndtp_host, settings.ndtp_port, engine.on_frame,
                        settings.ndtp_verify_crc, settings.ndtp_idle_timeout_s)
    await server.start()
    task = asyncio.create_task(engine.run(), name="predict-loop")
    state.update(ref=ref, ml=ml, engine=engine, server=server, task=task,
                 startup_s=round(time.perf_counter() - t0, 2))
    log.info("backend ready in %.2fs", state["startup_s"])
    yield
    task.cancel()
    await server.stop()
    await ml.close()


app = FastAPI(
    title="Mos-Transport Dispatcher Backend",
    version="1.0.0",
    description=(
        "Приём потока телеметрии NDTP (TCP), сопоставление с эталонным расписанием, расчёт производных "
        "признаков, оркестрация прогнозов ML-сервиса (горизонт 10–15 мин), риск/инциденты для диспетчера.\n\n"
        "WebSocket `/ws` — поток снимков состояния для дашборда (≈1 раз в секунду)."
    ),
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _engine() -> Engine:
    eng = state.get("engine")
    if eng is None:
        raise HTTPException(503, "starting")
    return eng


# ------------------------------------------------------------------ service
@app.get("/health", tags=["service"], summary="Liveness/readiness")
def health():
    eng = state.get("engine")
    return {"status": "ok" if eng else "starting", "uptime_s": round(time.time() - STARTED, 1),
            "startup_s": state.get("startup_s"), **({"mode": eng.status()["mode"]} if eng else {})}


@app.get("/api/status", tags=["service"], summary="Режим работы системы (online / degraded) и причины")
def status():
    return _engine().status()


@app.get("/api/metrics", tags=["service"], summary="Метрики производительности, надёжности и онлайн-точности")
def metrics():
    eng = _engine()
    srv: NDTPServer = state["server"]
    return {
        **eng.metrics(),
        "ndtp": {"active_connections": len(srv.connections), "connections_total": srv.total_connections,
                 "disconnects_total": srv.total_disconnects, "parse_errors": srv.parse_errors, "skipped_garbage_bytes": srv.skipped_bytes,
                 "handler_errors": srv.handler_errors},
        "startup_s": state.get("startup_s"), "uptime_s": round(time.time() - STARTED, 1),
    }


@app.get("/metrics", tags=["service"], response_class=PlainTextResponse, summary="Prometheus-метрики")
def prometheus():
    m = metrics()
    lines = [
        f"ndtp_packets_total {m['ingest']['packets_total']}",
        f"ndtp_packets_per_second {m['ingest']['packets_per_s']}",
        f"ndtp_parse_errors_total {m['ndtp']['parse_errors']}",
        f"ndtp_active_connections {m['ndtp']['active_connections']}",
        f"ndtp_connections_total {m['ndtp']['connections_total']}",
        f"predict_cycles_total {m['prediction']['cycles']}",
        f"predict_cycle_overruns_total {m['prediction']['cycle_overruns']}",
        f"predictions_total {m['prediction']['predictions_total']}",
        f"predictions_fallback_total {m['prediction']['fallback_predictions']}",
        f"system_degraded {0 if m['status']['mode'] == 'online' else 1}",
    ]
    for name, key in (("ml_request_ms", m["prediction"]["ml_request_ms"]),
                      ("e2e_packet_to_prediction_ms", m["prediction"]["e2e_packet_to_prediction_ms"]),
                      ("ingest_processing_us", m["ingest"]["processing_us"])):
        for q in ("p50", "p95", "p99"):
            if key[q] is not None:
                lines.append(f'{name}{{quantile="{q}"}} {key[q]}')
    return "\n".join(lines) + "\n"


@app.get("/api/connections", tags=["ndtp"], summary="Активные NDTP-соединения терминалов")
def connections():
    srv: NDTPServer = state["server"]
    return [c.__dict__ for c in srv.connections.values()]


@app.post("/api/ndtp/parse", tags=["ndtp"], summary="Разобрать NDTP-кадр(ы) из hex (отладка парсера)")
def parse_ndtp(hex_data: str = Body(
        ...,
        media_type="text/plain",
        examples=["7e7e26000000c15c0200cc110000000100650001000200000000"
                  "001ba1b76a0d2b5f1609473821e0842d0030003d01060df0000e02"],
        description="Один или несколько NDTP-кадров в hex (пробелы допускаются)")):
    try:
        raw = bytes.fromhex("".join(hex_data.split()))
    except ValueError as e:
        raise HTTPException(400, f"invalid hex: {e}") from e
    dec = ndtp.StreamDecoder()
    frames = []
    for f in dec.feed(raw):
        nav = f.nav()
        frames.append({
            "unit_id": f.peer_address, "service_id": f.service_id, "type": f.nph_type,
            "request_id": f.nph_request_id, "needs_reply": f.needs_reply,
            "kind": "handshake" if f.is_handshake else ("telemetry" if f.is_telemetry else "other"),
            "handshake": f.handshake, "cells": f.cells, "unparsed_cells": f.unparsed_cells,
            "nav": nav.__dict__ if nav else None,
        })
    return {"frames": frames, "errors": dec.errors, "last_error": dec.last_error}


# ------------------------------------------------------------------ dispatcher
@app.get("/api/state", tags=["dispatcher"], summary="Полный снимок: KPI, ТС, инциденты, риск участков")
def snapshot():
    snap = _engine().snapshot
    if not snap:
        raise HTTPException(503, "first prediction cycle has not completed yet")
    return snap


@app.get("/api/vehicles", tags=["dispatcher"], summary="Текущее положение и риск всех ТС")
def vehicles():
    return _engine().snapshot.get("vehicles", [])


@app.get("/api/vehicles/{vehicle_id}", tags=["dispatcher"],
         summary="Детали ТС (unit_id или tr_id): признаки модели, трейл прогнозов, факт прибытий")
def vehicle(vehicle_id: int):
    d = _engine().vehicle_detail(vehicle_id)
    if d is None:
        raise HTTPException(404, "vehicle not found")
    return d


@app.get("/api/incidents", tags=["dispatcher"], summary="Открытые инциденты (карточки) и недавно закрытые")
def incidents():
    snap = _engine().snapshot
    return {"open": snap.get("incidents", []), "resolved": snap.get("resolved", [])}


@app.post("/api/incidents/{incident_id}/ack", tags=["dispatcher"], summary="Принять инцидент в работу")
def ack(incident_id: str):
    if not _engine().acknowledge(incident_id):
        raise HTTPException(404, "incident not found")
    return {"ok": True}


@app.get("/api/network", tags=["dispatcher"], summary="Маршрутная сеть: остановки, сегменты, маршруты")
def network():
    ref = state["ref"]
    return {
        "stops": [{"key": s["key"], "lon": s["lon"], "lat": s["lat"], "address": s["address"],
                   "routes": sorted(s["routes"])} for s in ref.stops.values()],
        "segments": [{"id": s["id"], "coords": s["coords"], "routes": sorted(s["routes"])}
                     for s in ref.segments.values()],
        "routes": [{"id": r["id"], "vehicles": r["vehicles"]} for r in ref.routes.values()],
    }


@app.get("/api/model", tags=["dispatcher"], summary="Информация о модели (прокси ML-сервиса)")
async def model():
    ml: MLClient = state["ml"]
    await ml.refresh_info()
    if ml.model_info is None:
        raise HTTPException(503, f"ML service unavailable: {ml.last_error}")
    return ml.model_info


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    eng = _engine()
    q = eng.subscribe()
    try:
        if eng.snapshot:
            await websocket.send_json(eng.snapshot)
        while True:
            snap = await q.get()
            await websocket.send_json(snap)
    except Exception:  # noqa: BLE001 — клиент отключился
        pass
    finally:
        eng.unsubscribe(q)
