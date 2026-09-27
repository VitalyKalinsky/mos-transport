"""Расширенное API: регион/карта, поиск и отслеживание ТС, ETA, what-if, таймлайн, реестр маршрутов,
пробки/автобусные полосы, экспорт для картографических сервисов, управление потоком.

Эндпоинты регистрируются функцией :func:`register` на общем приложении из :mod:`app.main`.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from typing import Any, Literal

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import export
from .config import settings
from .routes_registry import ImportError_, geometry_by_router, import_gtfs, import_osm
from .whatif import SCENARIO_TYPES, merge

ScenarioType = Literal["reserve_bus", "detour", "signal_priority", "domino", "skip_stops",
                       "breakdown", "accident", "blockage", "traffic"]


class Scenario(BaseModel):
    """Один сценарий what-if: тип + параметры (см. ``GET /api/whatif/types``)."""
    type: ScenarioType
    params: dict[str, Any] = Field(default_factory=dict, examples=[{"unit_id": 985940, "minutes": 10}])


class SimulateRequest(BaseModel):
    """Запрос моделирования: сценарии применяются вместе (комбинация)."""
    scenarios: list[Scenario]
    horizon_stops: int = Field(12, ge=3, le=40)
    include_all: bool = False  # показать и незатронутые ТС (для сравнения по сети)


class ApplyRequest(SimulateRequest):
    """Запрос применения сценария к живому прогнозу."""
    duration_min: float = Field(30, description="Сколько минут (время данных) воздействие действует в живом прогнозе")
    inject_into_stream: bool = Field(True, description="Поломку/ДТП/засор также имитировать в потоке телеметрии (фидер)")


class ManualRoute(BaseModel):
    """Маршрут, заданный вручную списком остановок."""
    ref: str = Field(examples=["м6"])
    name: str = ""
    mode: Literal["bus", "trolleybus", "tram", "share_taxi"] = "bus"
    stops: list[dict] = Field(description="[{name, lat, lon}] в порядке следования", min_length=2)
    vehicles: list[int] = Field(default_factory=list)


class OsmImport(BaseModel):
    """Импорт из OSM по номеру или ID relation."""
    ref: str | None = Field(None, description="Номер маршрута как в OSM (например «м6», «т25», «297»)")
    relation_id: int | None = None
    vehicles: list[int] = Field(default_factory=list, description="Привязать ТС (tr_id) к импортированному маршруту")


class TrafficUpdate(BaseModel):
    """Внешний индекс пробок и автобусные полосы для набора перегонов."""
    segment_ids: list[str] = Field(default_factory=list)
    jam: float | None = Field(None, ge=0, le=10, description="Балл пробок 0–10 (как в картографических сервисах)")
    bus_lane: float | None = Field(None, ge=0, le=1, description="Доля участка с выделенной полосой ОТ")


def register(app: FastAPI, state: dict) -> None:
    """Регистрирует расширенные эндпоинты.

    Args:
        app: Приложение FastAPI.
        state: Общее состояние процесса из :mod:`app.main` (заполняется в lifespan).
    """
    def eng():
        e = state.get("engine")
        if e is None:
            raise HTTPException(503, "starting")
        return e

    # ------------------------------------------------------------- region / map
    @app.get("/api/region", tags=["map"], summary="Регион обслуживания: границы карты, центр, провайдеры тайлов")
    def region():
        """Регион карты: границы, центр, масштабы, часовой пояс, провайдеры тайлов (2ГИС, Яндекс, OSM)."""
        return state["region"]

    # ------------------------------------------------------------- search / tracking
    @app.get("/api/search", tags=["dispatcher"], summary="Поиск ТС по номеру ТС / бортовому терминалу / номеру маршрута")
    def search(q: str = Query(..., min_length=1, examples=["м6"])):
        """Поиск ТС и маршрутов.

        Args:
            q: tr_id, unit_id или номер маршрута. Короткие запросы (< 3 символов) — только точное
                совпадение, чтобы «1» не находило всё подряд.

        Returns:
            dict: ``vehicles`` (до 30) и ``routes`` (до 10).
        """
        e = eng()
        ql = q.strip().lower()
        res = []
        for v in e.snapshot.get("vehicles", []):
            keys = [str(v["tr_id"] or ""), str(v["unit_id"]), (v.get("route_ref") or "").lower(),
                    (v.get("route_id") or "").lower()]
            if any(k and (k == ql or (len(ql) >= 3 and ql in k)) for k in keys):
                res.append({"kind": "vehicle", "unit_id": v["unit_id"], "tr_id": v["tr_id"],
                            "route_ref": v.get("route_ref"), "route_id": v.get("route_id"), "level": v["level"]})
        routes = state["registry"].search(q)
        return {"vehicles": res[:30], "routes": routes[:10]}

    @app.get("/api/vehicles/{vehicle_id}/eta", tags=["dispatcher"], summary="ETA на ближайшие остановки")
    def vehicle_eta(vehicle_id: int, stops: int = Query(8, ge=1, le=40)):
        """ETA на ближайшие остановки (с учётом применённых what-if).

        Args:
            vehicle_id: unit_id или tr_id.
            stops: Число остановок.

        Raises:
            HTTPException: 404 — ТС не найдено или без расписания.
        """
        e = eng()
        st = e.vehicles.get(vehicle_id) or next((v for v in e.vehicles.values() if v.tr_id == vehicle_id), None)
        if st is None or st.schedule is None:
            raise HTTPException(404, "vehicle not found or has no schedule")
        st.eta = e.eta.predict(st, e.clock.now(), stops, e.live_overrides)
        return e.eta_view(st)

    # ------------------------------------------------------------- what-if
    @app.get("/api/whatif/types", tags=["what-if"], summary="Типы сценариев и их параметры")
    def whatif_types():
        """Справочник сценариев what-if: название и параметры (в скобках — значения по умолчанию)."""
        return {
            "reserve_bus": {"title": SCENARIO_TYPES["reserve_bus"], "params": {"unit_id": "ТС", "dispatch_min": "минут до выхода резерва (8)"}},
            "detour": {"title": SCENARIO_TYPES["detour"], "params": {"unit_id": "ТС", "offset": "через сколько остановок начинается закрытие (0)", "closed_stops": "число закрытых остановок (1)", "side_m": "смещение объезда, м (450)"}},
            "signal_priority": {"title": SCENARIO_TYPES["signal_priority"], "params": {"unit_id": "ТС (участки впереди)", "gain": "сокращение времени перегона (0.15)", "segments": "число перегонов (10)"}},
            "domino": {"title": SCENARIO_TYPES["domino"], "params": {"unit_id": "ТС", "action": "short_turn | skip | reserve | hold"}},
            "skip_stops": {"title": SCENARIO_TYPES["skip_stops"], "params": {"unit_id": "ТС", "count": "число остановок (2)"}},
            "breakdown": {"title": SCENARIO_TYPES["breakdown"], "params": {"unit_id": "ТС", "minutes": "длительность простоя (10)"}},
            "accident": {"title": SCENARIO_TYPES["accident"], "params": {"unit_id": "ТС (ДТП на его пути)", "minutes": "длительность (20)", "capacity": "пропускная способность (0.35)"}},
            "blockage": {"title": SCENARIO_TYPES["blockage"], "params": {"unit_id": "ТС", "minutes": "длительность (20)", "capacity": "пропускная способность (0.6)"}},
            "traffic": {"title": SCENARIO_TYPES["traffic"], "params": {"unit_id": "ТС (участки впереди)", "jam": "балл пробок 0–10", "bus_lane": "есть выделенная полоса (bool)"}},
        }

    @app.post("/api/whatif/simulate", tags=["what-if"], summary="Смоделировать сценарий(и): сравнение до/после")
    async def whatif_simulate(req: SimulateRequest):
        """Моделирует сценарии поверх текущего состояния сети без изменения живого прогноза.

        Returns:
            dict: По каждому затронутому ТС — ETA «до/после» по остановкам, суммарное опоздание,
            остановки с опозданием > 2 мин, опаздывающие следующие рейсы; общий эффект и рекомендация.
        """
        res = await state["whatif"].simulate([s.model_dump() for s in req.scenarios], req.horizon_stops, req.include_all)
        res.pop("_overrides", None)  # внутренний объект, не сериализуется
        return res

    @app.post("/api/whatif/apply", tags=["what-if"], summary="Применить сценарий к живому прогнозу (и потоку)")
    async def whatif_apply(req: ApplyRequest):
        """Применяет сценарий к живому прогнозу на ``duration_min`` минут времени данных.

        Поломка, ДТП и засор дополнительно передаются в фидер: ТС реально останавливается или
        замедляется в потоке NDTP, и весь контур (детектор, ML, инциденты) реагирует на настоящее опоздание.

        Returns:
            dict: ``until``, результат инъекции в поток, эффект и рекомендация.
        """
        e = eng()
        res = await state["whatif"].simulate([s.model_dump() for s in req.scenarios], req.horizon_stops)
        ov = res.pop("_overrides")
        until = e.clock.now() + req.duration_min * 60  # срок во времени данных: при ускорении реплея истекает быстрее
        e.live_ovs.append((ov, until))
        e.live_overrides = merge(e.live_overrides, ov)
        e.live_scenarios.append({"scenarios": [s.model_dump() for s in req.scenarios], "until_T": until,
                                 "until": e._local(until), "title": ", ".join(SCENARIO_TYPES[s.type] for s in req.scenarios)})
        injected = []
        if req.inject_into_stream:
            for s in req.scenarios:
                unit = s.params.get("unit_id")
                if s.type in ("breakdown", "accident", "blockage") and unit:
                    kind = "breakdown" if s.type == "breakdown" else "slowdown"
                    q = f"minutes={s.params.get('minutes', 10 if kind == 'breakdown' else 20)}"
                    if kind == "slowdown":
                        # пропускная способность 0.35 → ТС едет в 1/0.35 ≈ 2.9 раза медленнее
                        q += f"&factor={1 / max(0.1, float(s.params.get('capacity', 0.35 if s.type == 'accident' else 0.6))):.2f}"
                    ok = await asyncio.to_thread(_feeder_post, f"/incident/{kind}/{unit}?{q}")
                    injected.append({"unit_id": unit, "kind": kind, "ok": ok})
        e._event("warn", f"Применён сценарий: {e.live_scenarios[-1]['title']} (до {e.live_scenarios[-1]['until']})")
        return {"applied": True, "until": e._local(until), "stream_injection": injected, "effect": res["effect"],
                "recommendation": res["recommendation"]}

    @app.delete("/api/whatif/live", tags=["what-if"], summary="Снять все применённые сценарии")
    async def whatif_clear():
        """Снимает все применённые сценарии и внештатные ситуации в потоке фидера."""
        e = eng()
        e.live_overrides = None
        e.live_ovs.clear()
        e.live_scenarios.clear()
        await asyncio.to_thread(_feeder_post, "/incident/clear")
        e._event("info", "Применённые сценарии сняты")
        return {"ok": True}

    # ------------------------------------------------------------- timeline
    @app.get("/api/timeline", tags=["timeline"], summary="История состояний (кадры) и прогноз положения ТС")
    def timeline(past_min: int = Query(120, ge=1, le=720), future_min: int = Query(30, ge=0, le=120),
                 step_s: int = Query(60, ge=15, le=600)):
        """Кадры истории и прогноз положения ТС для интерактивного таймлайна.

        Args:
            past_min: Глубина истории, мин (хранится до 6 ч).
            future_min: Горизонт прогноза положения, мин.
            step_s: Шаг между кадрами, с (прореживание для размера ответа).

        Returns:
            dict: ``now``, ``past`` (кадры), ``future`` (прогнозные кадры по ETA).
        """
        e = eng()
        T = e.clock.now()
        frames, last = [], -1e18
        for f in e.timeline:
            if f["T"] >= T - past_min * 60 and f["T"] - last >= step_s:
                frames.append(f)
                last = f["T"]
        fut = e.future_frames(future_min, step_s) if future_min else []
        return {"now_T": T, "now": e._local(T), "past": frames, "future": fut}

    # ------------------------------------------------------------- traffic / bus lanes
    @app.put("/api/traffic", tags=["map"], summary="Внешний индекс пробок / автобусные полосы для участков")
    def traffic(u: TrafficUpdate):
        """Задаёт внешний индекс пробок и/или долю автобусной полосы для перегонов (влияет на ETA и what-if)."""
        e = eng()
        for sid in u.segment_ids:
            if u.jam is not None:
                e.eta.manual_traffic[sid] = 1.0 + 0.12 * u.jam  # 10 баллов ≈ перегон в 2.2 раза дольше
            if u.bus_lane is not None:
                e.eta.bus_lanes[sid] = u.bus_lane
        return {"segments": len(u.segment_ids), "manual_traffic": len(e.eta.manual_traffic),
                "bus_lanes": len(e.eta.bus_lanes)}

    @app.get("/api/traffic", tags=["map"], summary="Текущая загруженность перегонов (живая + внешняя)")
    def traffic_get():
        """Загруженность перегонов: ``live`` (по проездам наших ТС), ``manual`` (внешний индекс), ``bus_lanes``."""
        e = eng()
        return {"live": {k: round(v[0], 2) for k, v in e.eta.traffic.items()},
                "manual": e.eta.manual_traffic, "bus_lanes": e.eta.bus_lanes}

    # ------------------------------------------------------------- routes registry
    reg = lambda: state["registry"]  # noqa: E731 — реестр появляется только после lifespan

    def _relink():
        eng().route_names = reg().vehicle_links()  # номера маршрутов на карте меняются сразу

    @app.get("/api/routes", tags=["routes"], summary="Реестр реальных маршрутов")
    def routes_list():
        """Все маршруты реестра (без геометрии)."""
        return [reg().summary(r) for r in reg().routes.values()]

    @app.get("/api/routes/{route_id}", tags=["routes"], summary="Маршрут: остановки и геометрия")
    def route_get(route_id: str):
        """Полная запись маршрута.

        Raises:
            HTTPException: 404 — маршрут не найден.
        """
        r = reg().routes.get(route_id)
        if r is None:
            raise HTTPException(404, "route not found")
        return r

    @app.post("/api/routes/import/osm", tags=["routes"], summary="Импорт маршрута из OpenStreetMap по номеру/relation")
    async def route_import_osm(req: OsmImport):
        """Импортирует все направления маршрута из OSM и сохраняет в реестр.

        Raises:
            HTTPException: 502 — Overpass недоступен или маршрут не найден.
        """
        try:
            variants = await asyncio.to_thread(import_osm, req.ref, req.relation_id, state["region"]["bounds"],
                                               state["region"].get("osm_area"))
        except ImportError_ as e:
            raise HTTPException(502, str(e)) from e
        out = []
        for v in variants:
            v["vehicles"] = req.vehicles
            out.append(reg().summary(reg().save(v)))
        _relink()
        return out

    @app.post("/api/routes/import/gtfs", tags=["routes"], summary="Импорт маршрутов из GTFS (zip)")
    async def route_import_gtfs(file: UploadFile = File(...)):
        """Импортирует маршруты из GTFS-архива перевозчика.

        Raises:
            HTTPException: 400 — архив некорректен или без рейсов.
        """
        blob = await file.read()
        try:
            routes = await asyncio.to_thread(import_gtfs, blob)
        except ImportError_ as e:
            raise HTTPException(400, str(e)) from e
        return [reg().summary(reg().save(r)) for r in routes]

    @app.post("/api/routes", tags=["routes"], summary="Создать маршрут по списку остановок (геометрия по дорогам)")
    async def route_create(r: ManualRoute):
        """Создаёт маршрут вручную; геометрия строится роутером (если он недоступен — прямыми)."""
        geom, src = await asyncio.to_thread(geometry_by_router, r.stops)
        rid = f"manual-{r.ref}-{int(time.time())}"
        route = {"id": rid, "ref": r.ref, "name": r.name or f"Маршрут {r.ref}", "mode": r.mode,
                 "from": r.stops[0].get("name", ""), "to": r.stops[-1].get("name", ""), "source": f"manual/{src}",
                 "stops": r.stops, "geometry": geom, "vehicles": r.vehicles}
        out = reg().summary(reg().save(route))
        _relink()
        return out

    @app.put("/api/routes/{route_id}/vehicles", tags=["routes"], summary="Привязать ТС (tr_id) к маршруту")
    def route_vehicles(route_id: str, vehicles: list[int] = Body(..., examples=[[131672]])):
        """Заменяет список привязанных ТС.

        Raises:
            HTTPException: 404 — маршрут не найден.
        """
        r = reg().routes.get(route_id)
        if r is None:
            raise HTTPException(404, "route not found")
        r["vehicles"] = sorted(set(vehicles))
        reg().save(r)
        _relink()
        return reg().summary(r)

    @app.delete("/api/routes/{route_id}", tags=["routes"], summary="Удалить маршрут")
    def route_delete(route_id: str):
        """Удаляет маршрут из реестра и с диска.

        Raises:
            HTTPException: 404 — маршрут не найден.
        """
        if not reg().delete(route_id):
            raise HTTPException(404, "route not found")
        _relink()
        return {"ok": True}

    # ------------------------------------------------------------- export (integrations)
    def _rt(kind: str, fmt: str):
        d = export.feed(eng(), kind)
        if fmt == "json":
            return JSONResponse(d)
        try:
            return Response(export.to_protobuf(d), media_type="application/x-protobuf")
        except RuntimeError as e:
            raise HTTPException(501, str(e)) from e  # нет protobuf-биндингов — JSON остаётся доступен

    @app.get("/api/export/gtfs-rt/vehicle-positions", tags=["export"], summary="GTFS-Realtime: положения ТС")
    def gtfs_rt_vp(format: Literal["pb", "json"] = "pb"):
        """GTFS-RT VehiclePositions (protobuf или JSON).

        Raises:
            HTTPException: 501 — protobuf недоступен (используйте ``format=json``).
        """
        return _rt("vehicle_positions", format)

    @app.get("/api/export/gtfs-rt/trip-updates", tags=["export"], summary="GTFS-Realtime: прогноз прибытия (ETA + ML)")
    def gtfs_rt_tu(format: Literal["pb", "json"] = "pb"):
        """GTFS-RT TripUpdates: ETA-движок, на целевой остановке — ML-прогноз.

        Raises:
            HTTPException: 501 — protobuf недоступен (используйте ``format=json``).
        """
        return _rt("trip_updates", format)

    @app.get("/api/export/gtfs-rt/alerts", tags=["export"], summary="GTFS-Realtime: инциденты")
    def gtfs_rt_alerts(format: Literal["pb", "json"] = "pb"):
        """GTFS-RT Alerts по открытым инцидентам.

        Raises:
            HTTPException: 501 — protobuf недоступен (используйте ``format=json``).
        """
        return _rt("alerts", format)

    @app.get("/api/export/gtfs-static.zip", tags=["export"], summary="Статический GTFS (идентификаторы совпадают с RT)")
    async def gtfs_static():
        """Статический GTFS (zip) по эталонному расписанию; ID совпадают с realtime-фидами."""
        blob = await asyncio.to_thread(export.gtfs_static, eng())  # сборка zip ~0.1 с — не в event loop
        return Response(blob, media_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=mos-transport-gtfs.zip"})

    @app.get("/api/export/vehicles.geojson", tags=["export"], summary="GeoJSON ТС для JS API карт")
    def vehicles_geojson():
        """GeoJSON FeatureCollection ТС с уровнем риска и прогнозом."""
        return JSONResponse(export.geojson(eng()), media_type="application/geo+json")


def _feeder_post(path: str) -> bool:
    """POST в фидер (инъекция внештатных ситуаций в поток).

    Args:
        path: Путь с query-строкой.

    Returns:
        bool: ``True`` — фидер принял команду; ``False`` при любой ошибке (исключения не пробрасываются).
    """
    try:
        req = urllib.request.Request(settings.feeder_url + path, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:  # короткий таймаут: фидер локальный
            json.loads(r.read() or b"{}")
        return True
    except Exception:  # noqa: BLE001 — фидера может не быть (реальные терминалы)
        return False
