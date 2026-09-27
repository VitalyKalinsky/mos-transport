"""Backend: приём NDTP, сопоставление с расписанием, оркестрация прогнозов, REST API и WebSocket дашборда.

Точка входа: ``uvicorn app.main:app``. Swagger: http://localhost:8000/docs.
Расширенные эндпоинты (ETA, what-if, маршруты, экспорт) регистрируются в :mod:`app.api_ext`.
"""
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
from .regions import load_region
from .routes_registry import RouteRegistry
from .whatif import WhatIf
from . import api_ext

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # иначе лог на каждый батч к ML
log = logging.getLogger("backend")

STARTED = time.time()
state: dict = {}  # общие объекты процесса; заполняется в lifespan, пуст до готовности (→ 503)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Запуск: справочники → ML-клиент → движок → NDTP-сервер → цикл прогноза; при остановке — обратный порядок.

    ML-сервис при старте не требуется: backend поднимается и работает на baseline, пока ML недоступен.
    """
    t0 = time.perf_counter()
    # чтение CSV блокирующее — в отдельном потоке, чтобы не держать event loop
    ref = await asyncio.to_thread(load_reference, settings.schedule_path, settings.units_path)
    ml = MLClient(settings)
    engine = Engine(settings, ref, ml)
    registry = RouteRegistry(settings.routes_dir, settings.builtin_routes_dir)
    engine.route_names = registry.vehicle_links()
    region = load_region(settings.regions_path, settings.region)
    server = NDTPServer(settings.ndtp_host, settings.ndtp_port, engine.on_frame,
                        settings.ndtp_verify_crc, settings.ndtp_idle_timeout_s)
    await server.start()
    task = asyncio.create_task(engine.run(), name="predict-loop")
    state.update(ref=ref, ml=ml, engine=engine, server=server, task=task, registry=registry,
                 region=region, whatif=WhatIf(engine),
                 startup_s=round(time.perf_counter() - t0, 2))
    log.info("backend ready in %.2fs", state["startup_s"])
    yield
    task.cancel()
    await server.stop()
    await ml.close()


app = FastAPI(
    title="Mos-Transport Dispatcher Backend",
    version="2.0.0",
    description=(
        "Приём потока телеметрии NDTP (TCP), сопоставление с эталонным расписанием, расчёт производных "
        "признаков, оркестрация прогнозов ML-сервиса (горизонт 10–15 мин), риск/инциденты для диспетчера.\n\n"
        "WebSocket `/ws` — поток снимков состояния для дашборда (≈1 раз в секунду).\n\n"
        "Расширения: ETA на ближайшие остановки, what-if моделирование, таймлайн, реестр реальных маршрутов "
        "(OSM/GTFS), экспорт GTFS-Realtime/GTFS/GeoJSON для картографических сервисов."
    ),
    lifespan=lifespan,
)
# CORS открыт: фиды /api/export/* забирают внешние карты; в проде доступ режется на балансировщике
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
api_ext.register(app, state)


def _engine() -> Engine:
    """Движок или HTTP 503, пока идёт старт.

    Raises:
        HTTPException: 503 — lifespan ещё не завершился.
    """
    eng = state.get("engine")
    if eng is None:
        raise HTTPException(503, "starting")
    return eng


# ------------------------------------------------------------------ service
@app.get("/health", tags=["service"], summary="Liveness/readiness")
def health():
    """Проверка живости для Docker healthcheck.

    Всегда 200: при недоступном ML сервис жив и работает в деградированном режиме.

    Returns:
        dict: ``status`` (ok/starting), ``uptime_s``, ``startup_s``, ``mode`` (online/degraded).
    """
    eng = state.get("engine")
    return {"status": "ok" if eng else "starting", "uptime_s": round(time.time() - STARTED, 1),
            "startup_s": state.get("startup_s"), **({"mode": eng.status()["mode"]} if eng else {})}


@app.get("/api/status", tags=["service"], summary="Режим работы системы (online / degraded) и причины")
def status():
    """Режим работы: связь с телематикой, доступность ML, причины деградации.

    Raises:
        HTTPException: 503 — сервис стартует.
    """
    return _engine().status()


@app.get("/api/metrics", tags=["service"], summary="Метрики производительности, надёжности и онлайн-точности")
def metrics():
    """Метрики приёма NDTP, цикла прогноза, задержек (p50/p95/p99) и онлайн-точности модели.

    Returns:
        dict: Разделы ``ingest``, ``prediction``, ``accuracy_online``, ``status``, ``ndtp``.

    Raises:
        HTTPException: 503 — сервис стартует.
    """
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
    """Те же метрики в текстовом формате Prometheus (без клиентской библиотеки)."""
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
            if key[q] is not None:  # пустое окно сразу после старта
                lines.append(f'{name}{{quantile="{q}"}} {key[q]}')
    return "\n".join(lines) + "\n"


@app.get("/api/connections", tags=["ndtp"], summary="Активные NDTP-соединения терминалов")
def connections():
    """Активные TCP-соединения: терминал, байты, кадры, ошибки, версия протокола.

    Returns:
        list[dict]: Поля :class:`app.ndtp_server.ConnInfo`.
    """
    srv: NDTPServer = state["server"]
    return [c.__dict__ for c in srv.connections.values()]


@app.post("/api/ndtp/parse", tags=["ndtp"], summary="Разобрать NDTP-кадр(ы) из hex (отладка парсера)")
def parse_ndtp(hex_data: str = Body(
        ...,
        media_type="text/plain",
        examples=["7e7e26000000c15c0200cc110000000100650001000200000000"
                  "001ba1b76a0d2b5f1609473821e0842d0030003d01060df0000e02"],
        description="Один или несколько NDTP-кадров в hex (пробелы допускаются)")):
    """Разбирает NDTP-кадры тем же декодером, что и TCP-сервер. Пример — реальный кадр эмулятора.

    Args:
        hex_data: Кадры в hex; пробелы и переводы строк игнорируются.

    Returns:
        dict: ``frames`` (заголовки, ячейки, навигация), ``errors``, ``last_error``.

    Raises:
        HTTPException: 400 — строка не является hex.
    """
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
    """Полный снимок состояния; то же, что приходит по WebSocket ``/ws``.

    Raises:
        HTTPException: 503 — первый цикл прогноза ещё не завершён.
    """
    snap = _engine().snapshot
    if not snap:
        raise HTTPException(503, "first prediction cycle has not completed yet")
    return snap


@app.get("/api/vehicles", tags=["dispatcher"], summary="Текущее положение и риск всех ТС")
def vehicles():
    """ТС: положение, уровень риска, прогноз на T+10…15 мин, P(опоздание), причина, ближайшие остановки.

    Returns:
        list[dict]: Пустой список до первого цикла прогноза.
    """
    return _engine().snapshot.get("vehicles", [])


@app.get("/api/vehicles/{vehicle_id}", tags=["dispatcher"],
         summary="Детали ТС (unit_id или tr_id): признаки модели, трейл прогнозов, факт прибытий")
def vehicle(vehicle_id: int):
    """Карточка ТС: прогноз, SHAP-объяснение, признаки модели, ETA, история прогнозов и прибытий.

    Args:
        vehicle_id: unit_id терминала или tr_id ТС.

    Raises:
        HTTPException: 404 — ТС не найдено.
    """
    d = _engine().vehicle_detail(vehicle_id)
    if d is None:
        raise HTTPException(404, "vehicle not found")
    return d


@app.get("/api/incidents", tags=["dispatcher"], summary="Открытые инциденты (карточки) и недавно закрытые")
def incidents():
    """Карточки инцидентов: ТС, прогноз опоздания, P(late), причина, участок, целевая остановка, SHAP.

    Returns:
        dict: ``open`` — активные, ``resolved`` — недавно закрытые.
    """
    snap = _engine().snapshot
    return {"open": snap.get("incidents", []), "resolved": snap.get("resolved", [])}


@app.post("/api/incidents/{incident_id}/ack", tags=["dispatcher"], summary="Принять инцидент в работу")
def ack(incident_id: str):
    """Отмечает инцидент принятым диспетчером.

    Args:
        incident_id: ID инцидента из ``/api/incidents``.

    Raises:
        HTTPException: 404 — инцидент не найден или уже закрыт.
    """
    if not _engine().acknowledge(incident_id):
        raise HTTPException(404, "incident not found")
    return {"ok": True}


@app.get("/api/network", tags=["dispatcher"], summary="Маршрутная сеть: остановки, сегменты, маршруты")
def network():
    """Маршрутная сеть для карты: остановки, сегменты по дорогам, маршруты расписания и реальные маршруты.

    Returns:
        dict: ``stops``, ``segments``, ``routes``, ``real_routes``.
    """
    ref = state["ref"]
    return {
        "stops": [{"key": s["key"], "lon": s["lon"], "lat": s["lat"], "address": s["address"],
                   "routes": sorted(s["routes"])} for s in ref.stops.values()],
        "segments": [{"id": s["id"], "coords": s["coords"], "routes": sorted(s["routes"])}
                     for s in ref.segments.values()],
        "routes": [{"id": r["id"], "vehicles": r["vehicles"]} for r in ref.routes.values()],
        "real_routes": [dict(state["registry"].summary(r), geometry=r.get("geometry", []))
                        for r in state["registry"].routes.values()],
    }


@app.get("/api/model", tags=["dispatcher"], summary="Информация о модели (прокси ML-сервиса)")
async def model():
    """Метаданные модели из ML-сервиса: деревья, признаки, важности, валидационная MAE.

    Raises:
        HTTPException: 503 — ML-сервис недоступен.
    """
    ml: MLClient = state["ml"]
    await ml.refresh_info()
    if ml.model_info is None:
        raise HTTPException(503, f"ML service unavailable: {ml.last_error}")
    return ml.model_info


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    """Поток снимков состояния для дашборда (≈1 раз в секунду).

    Первым сообщением сразу уходит текущий снимок: дашборд не ждёт следующий цикл.
    """
    await websocket.accept()
    eng = _engine()
    q = eng.subscribe()  # очередь длиной 2: медленный клиент получает только свежий снимок
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
