"""Оркестрация: поток NDTP → состояние ТС → признаки → ML-прогноз → риск/инциденты → дашборд."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import ndtp

from .clock import DataClock
from .config import Settings
from .metrics import LatencyWindow, RateCounter
from .ml_client import MLClient
from .ndtp_server import ConnInfo
from .reference import Reference, seg_id
from .risk import diagnose, laplace_sf, risk_level
from .tracker import VehicleState, fmt_utc

log = logging.getLogger("backend.engine")

LEVEL_ORDER = {"red": 3, "yellow": 2, "green": 1, "gray": 0}


class Engine:
    def __init__(self, cfg: Settings, ref: Reference, ml: MLClient) -> None:
        self.cfg, self.ref, self.ml = cfg, ref, ml
        self.clock = DataClock(cfg.clock_freerun_max_s)
        self.vehicles: dict[int, VehicleState] = {}
        self.tz = timezone(timedelta(hours=cfg.display_tz_offset_h))

        # приём
        self.packets = RateCounter()
        self.frames_total = 0
        self.handshakes = 0
        self.history_packets = 0
        self.unknown_units: set[int] = set()
        self.ingest_us = LatencyWindow()
        self.last_packet_wall: float | None = None
        self.ever_connected = False

        # прогноз
        self.cycles = 0
        self.cycle_overruns = 0
        self.cycle_ms = LatencyWindow()
        self.e2e_ms = LatencyWindow()
        self.predictions_total = 0
        self.fallback_predictions = 0
        self.acc_model: deque[float] = deque(maxlen=5000)
        self.acc_base: deque[float] = deque(maxlen=5000)

        # инциденты и события
        self.incidents: dict[str, dict] = {}
        self.resolved: deque[dict] = deque(maxlen=100)
        self.acked: set[str] = set()
        self.events: deque[dict] = deque(maxlen=100)
        self._link_state = "waiting"
        self._ml_state: bool | None = None
        self.segment_levels: dict[str, str] = {}
        self.snapshot: dict[str, Any] = {}
        self.subscribers: set[asyncio.Queue] = set()
        self._new_data = asyncio.Event()

    # ================================================================ ingest
    def on_frame(self, frame: ndtp.Frame, conn: ConnInfo, rx_wall: float) -> None:
        t0 = time.perf_counter()
        self.frames_total += 1
        self.last_packet_wall = rx_wall
        if frame.is_handshake:
            self.handshakes += 1
            return
        if not frame.is_telemetry:
            return
        nav = frame.nav()
        if nav is None:
            return
        self.packets.hit(1, rx_wall)
        if frame.is_history:
            self.history_packets += 1

        unit = frame.peer_address
        st = self.vehicles.get(unit)
        if st is None:
            tr = self.ref.tr_for_unit(unit)
            if tr is None:
                self.unknown_units.add(unit)
            st = VehicleState(unit, tr, self.ref.schedules.get(tr) if tr is not None else None, self.cfg)
            self.vehicles[unit] = st
        if st.tr_id is not None:
            self.clock.observe(float(nav.timestamp), rx_wall)

        arrivals = st.ingest(float(nav.timestamp), nav.lon, nav.lat, nav.location_valid,
                             nav.speed, nav.heading, nav.alt, frame.is_history, rx_wall)
        for arr in arrivals:
            self._evaluate(st, arr)
        self.ingest_us.add((time.perf_counter() - t0) * 1e6)
        if st.schedule is not None:
            self._new_data.set()

    def _evict(self, wall: float) -> None:
        """Терминалы без наряда, молчащие > unknown_unit_ttl_s, убираем из оперативной картины
        (ТС с нарядом не удаляются — для них важно последнее известное состояние)."""
        ttl = self.cfg.unknown_unit_ttl_s
        for unit in [u for u, st in self.vehicles.items()
                     if st.schedule is None and wall - st.last_rx_wall > ttl]:
            del self.vehicles[unit]

    def _evaluate(self, st: VehicleState, arr) -> None:
        """Онлайн-оценка точности: сравнение прогнозов, сделанных за 10–15 мин, с фактом прибытия."""
        # прогнозы по остановкам, пропущенным детектором, оценить нельзя
        for idx in [i for i in st.pending_eval if i < arr.idx]:
            st.pending_eval.pop(idx, None)
        preds = st.pending_eval.pop(arr.idx, None)
        if not preds or not arr.detected:
            return
        for _T, pred, cur in preds:
            self.acc_model.append(abs(pred - arr.delay_s))
            self.acc_base.append(abs(cur - arr.delay_s))

    # ============================================================== predict
    async def run(self) -> None:
        await self.ml.refresh_info()
        while True:
            started = time.perf_counter()
            try:
                await self.predict_cycle()
            except Exception:  # noqa: BLE001 — цикл прогноза не должен падать никогда
                log.exception("predict cycle failed")
            elapsed = time.perf_counter() - started
            self.cycle_ms.add(elapsed * 1000)
            if elapsed > self.cfg.predict_interval_s:
                self.cycle_overruns += 1
            # Событийный запуск: новый пакет → прогноз не позже чем через min_interval;
            # без данных цикл всё равно идёт раз в predict_interval_s (деградация, часы, статусы).
            # Пакеты, пришедшие во время цикла, коалесцируются в следующий батч — очередь не копится.
            await asyncio.sleep(max(0.0, self.cfg.predict_min_interval_s - elapsed))
            self._new_data.clear()
            try:
                await asyncio.wait_for(self._new_data.wait(),
                                       timeout=max(0.0, self.cfg.predict_interval_s - self.cfg.predict_min_interval_s))
            except asyncio.TimeoutError:
                pass

    async def predict_cycle(self) -> None:
        wall = time.time()
        T = self.clock.now(wall)
        self.cycles += 1
        if self.cycles % 30 == 0 and self.ml.model_info is None:
            await self.ml.refresh_info()
        if self.cycles % 30 == 0:
            self._evict(wall)

        points, telemetry, ctx = [], [], []
        for st in self.vehicles.values():
            if st.schedule is None or st.last_ts == 0:
                continue
            k = st.target(T)
            if k < 0:
                st.prediction = {"status": "no_target", "T": T}
                continue
            cur_dev, method = st.current_deviation(T)
            stop = st.schedule.stops[k]
            sid = f"{st.tr_id}_{int(T)}"
            points.append({"sample_id": sid, "tr_id": st.tr_id, "T": fmt_utc(T),
                           "target_stop_id": stop.item_id, "target_time_begin": fmt_utc(stop.plan_ts),
                           "cur_dev_s": round(cur_dev, 3)})
            telemetry.extend(st.ml_history(T, self.cfg.ml_history_rows))
            ctx.append((st, k, cur_dev, method, st.dirty, st.last_rx_wall))
            st.dirty = False

        preds = await self.ml.predict(points, telemetry) if points else []
        source = "model"
        if preds is None:
            source = "fallback"
            preds = [{"predicted_delay_s": p["cur_dev_s"], "predicted_delta_s": 0.0, "features": {}} for p in points]
            self.fallback_predictions += len(points)
        publish_wall = time.time()

        for (st, k, cur_dev, method, dirty, rx_wall), pred in zip(ctx, preds):
            self._apply_prediction(st, k, T, cur_dev, method, pred, source)
            if dirty and rx_wall:
                self.e2e_ms.add((publish_wall - rx_wall) * 1000)
        self.predictions_total += len(points)

        self._update_status(wall)
        self._update_incidents(T)
        self._build_snapshot(T, wall)
        self._broadcast()

    def _apply_prediction(self, st: VehicleState, k: int, T: float, cur_dev: float, method: str,
                          pred: dict, source: str) -> None:
        cfg = self.cfg
        stop = st.schedule.stops[k]
        p = float(pred["predicted_delay_s"])
        telemetry_age = max(0.0, T - st.last_ts)
        p_late = laplace_sf(cfg.late_threshold_s, p, self.ml.error_scale_s)
        level = risk_level(p, p_late, cfg)
        seg = st.segment_info(T)
        dwell = st.dwell_s(T)
        hour_local = datetime.fromtimestamp(T, tz=self.tz).hour
        causes = diagnose(
            pred=p, cur_dev=cur_dev, dwell_s=dwell, at_stop=st.at_stop(), seg=seg, speed=st.speed,
            telemetry_age_s=telemetry_age, gps_fail_streak=st.gps_fail_streak,
            trend=st.deviation_trend(), is_peak=hour_local in (7, 8, 9, 10, 17, 18, 19, 20),
            source=source, cfg=cfg,
        )
        st.prediction = {
            "status": "ok", "T": T, "source": source,
            "predicted_delay_s": p, "predicted_delta_s": float(pred.get("predicted_delta_s", 0.0)),
            "p_late": p_late, "level": level,
            "cur_dev_s": cur_dev, "cur_dev_method": method,
            "target_idx": k, "target_stop_id": stop.item_id, "target_address": stop.address,
            "target_plan_ts": stop.plan_ts, "target_stop_key": stop.stop_key,
            "segment": seg, "dwell_s": dwell, "telemetry_age_s": telemetry_age,
            "causes": [c.__dict__ for c in causes[:3]],
            "features": pred.get("features", {}),
        }
        # трейл для графика и отложенная оценка точности (1 прогноз на минуту данных)
        if not st.pred_trail or T - st.pred_trail[-1][0] >= 30:
            st.pred_trail.append((T, cur_dev, p, p_late))
        lst = st.pending_eval.setdefault(k, [])
        if not lst or T - lst[-1][0] >= 60:
            lst.append((T, p, cur_dev))

    # ============================================================ status
    def _update_status(self, wall: float) -> None:
        if self.last_packet_wall is None:
            link = "waiting"
        elif wall - self.last_packet_wall <= self.cfg.link_timeout_s:
            link = "online"
        else:
            link = "lost"
        if link != self._link_state:
            if link == "lost":
                self._event("error", "Потеряна связь с источником телематики — работа по последнему "
                                     "известному состоянию и историческим данным")
            elif link == "online" and self._link_state == "lost":
                self._event("info", "Связь с источником телематики восстановлена")
            elif link == "online":
                self._event("info", "Поток телематики NDTP подключён")
            self._link_state = link
        ml_ok = self.ml.available and not self.ml.breaker_open
        if self._ml_state is None and not ml_ok and self.ml.failures == 0:
            return  # ML ещё ни разу не вызывался — не шумим в журнале при старте
        if ml_ok != self._ml_state:
            if self._ml_state is not None or not ml_ok:
                self._event("info" if ml_ok else "error",
                            "ML-сервис доступен" if ml_ok else "ML-сервис недоступен — прогноз по baseline (текущее отклонение)")
            self._ml_state = ml_ok

    def _event(self, level: str, text: str) -> None:
        self.events.appendleft({"wall": time.time(), "level": level, "text": text})
        (log.warning if level == "error" else log.info)(text)

    def status(self) -> dict:
        wall = time.time()
        reasons = []
        if self._link_state == "lost":
            reasons.append("Нет связи с телематикой: прогноз по последнему известному состоянию и истории")
        if self._link_state == "waiting":
            reasons.append("Ожидание потока телематики NDTP")
        if not self._ml_state:
            reasons.append("ML-сервис недоступен: прогноз = текущее отклонение (baseline)")
        mode = "online" if not reasons else ("waiting" if self._link_state == "waiting" and self._ml_state else "degraded")
        return {
            "mode": mode, "reasons": reasons, "link": self._link_state,
            "last_packet_age_s": None if self.last_packet_wall is None else round(wall - self.last_packet_wall, 1),
            "ml_available": bool(self._ml_state), "ml_breaker_open": self.ml.breaker_open,
            "ml_last_error": self.ml.last_error,
            "clock_rate": round(self.clock.rate, 2), "clock_freerun_s": round(self.clock.freerun_s(wall), 1),
        }

    # ============================================================ incidents
    def _update_incidents(self, T: float) -> None:
        active: set[str] = set()
        for st in self.vehicles.values():
            p = st.prediction
            if not p or p.get("status") != "ok":
                continue
            key = str(st.tr_id)
            inc = self.incidents.get(key)
            if p["level"] in ("red", "yellow"):
                active.add(key)
                if inc is None:
                    inc = {"id": f"{st.tr_id}-{int(T)}", "key": key, "tr_id": st.tr_id, "unit_id": st.unit_id,
                           "opened_T": T, "opened_wall": time.time(), "peak_delay_s": p["predicted_delay_s"],
                           "peak_level": p["level"]}
                    self.incidents[key] = inc
                    if p["level"] == "red":
                        self._event("warn", f"ТС {st.tr_id}: прогноз опоздания {int(p['predicted_delay_s'])} с — {p['causes'][0]['title']}")
                inc["green_since"] = None
                inc["peak_delay_s"] = max(inc["peak_delay_s"], p["predicted_delay_s"])
                if LEVEL_ORDER[p["level"]] > LEVEL_ORDER[inc["peak_level"]]:
                    inc["peak_level"] = p["level"]
            elif inc is not None:
                # гистерезис: закрываем после 2 мин (время данных) в «зелёной» зоне
                inc["green_since"] = inc.get("green_since") or T
                if T - inc["green_since"] >= 120:
                    inc["closed_T"] = T
                    self.resolved.appendleft(self._incident_view(inc, st, final=True))
                    self.incidents.pop(key, None)
                    self.acked.discard(inc["id"])
                else:
                    active.add(key)

    def _incident_view(self, inc: dict, st: VehicleState, final: bool = False) -> dict:
        p = st.prediction or {}
        seg = p.get("segment") or {}
        return {
            "id": inc["id"], "tr_id": st.tr_id, "unit_id": st.unit_id,
            "route_id": st.schedule.route_id if st.schedule else None,
            "level": p.get("level", "gray"), "peak_level": inc["peak_level"],
            "predicted_delay_s": _r(p.get("predicted_delay_s")), "peak_delay_s": _r(inc["peak_delay_s"]),
            "p_late": _r(p.get("p_late"), 3), "cur_dev_s": _r(p.get("cur_dev_s")),
            "source": p.get("source"),
            "target": {"stop_id": p.get("target_stop_id"), "address": p.get("target_address"),
                       "plan": self._local(p.get("target_plan_ts")),
                       "eta": self._local((p.get("target_plan_ts") or 0) + (p.get("predicted_delay_s") or 0))
                       if p.get("target_plan_ts") else None},
            "segment": {"from": (seg.get("from") or {}).get("address"), "to": (seg.get("to") or {}).get("address"),
                        "avg_speed_kmh": _r(seg.get("avg_speed_kmh"), 1),
                        "plan_speed_kmh": _r(seg.get("plan_speed_kmh"), 1)},
            "causes": p.get("causes", []),
            "dwell_s": _r(p.get("dwell_s")), "telemetry_age_s": _r(p.get("telemetry_age_s")),
            "lon": st.lon, "lat": st.lat,
            "opened": self._local(inc["opened_T"]), "opened_T": inc["opened_T"],
            "closed": self._local(inc.get("closed_T")) if final else None,
            "acknowledged": inc["id"] in self.acked,
            "recovering": bool(inc.get("green_since")),
        }

    def acknowledge(self, incident_id: str) -> bool:
        for inc in self.incidents.values():
            if inc["id"] == incident_id:
                self.acked.add(incident_id)
                self._event("info", f"Инцидент ТС {inc['tr_id']} принят в работу диспетчером")
                return True
        return False

    # ============================================================ snapshot
    def _local(self, ts: float | None) -> str | None:
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=self.tz).strftime("%H:%M:%S")

    def vehicle_view(self, st: VehicleState, T: float) -> dict:
        p = st.prediction or {}
        stale = (T - st.last_ts) > self.cfg.vehicle_stale_s if st.last_ts else True
        if st.schedule is None:
            level = "gray"
        elif p.get("status") == "ok":
            level = p["level"]
        else:
            level = "gray"
        state = "stale" if stale else ("no_gps" if not st.location_valid else
                                       ("standing" if st.speed < self.cfg.standing_speed_kmh else "moving"))
        seg = p.get("segment") or {}
        return {
            "unit_id": st.unit_id, "tr_id": st.tr_id,
            "route_id": st.schedule.route_id if st.schedule else None,
            "scheduled": st.schedule is not None,
            "lon": st.lon, "lat": st.lat, "heading": st.heading, "speed": _r(st.speed, 1),
            "state": state, "level": level, "stale": stale,
            "last_seen": self._local(st.last_ts) if st.last_ts else None,
            "age_s": _r(T - st.last_ts) if st.last_ts else None,
            "predicted_delay_s": _r(p.get("predicted_delay_s")), "p_late": _r(p.get("p_late"), 3),
            "cur_dev_s": _r(p.get("cur_dev_s")), "source": p.get("source"),
            "cause": (p.get("causes") or [{}])[0].get("title") if p.get("status") == "ok" and level != "green" else None,
            "target_address": p.get("target_address"), "target_plan": self._local(p.get("target_plan_ts")),
            "seg_from": (seg.get("from") or {}).get("address"), "seg_to": (seg.get("to") or {}).get("address"),
            "no_target": p.get("status") == "no_target",
        }

    def vehicle_detail(self, key: int) -> dict | None:
        st = self.vehicles.get(key) or next((v for v in self.vehicles.values() if v.tr_id == key), None)
        if st is None:
            return None
        T = self.clock.now()
        p = st.prediction or {}
        return {
            **self.vehicle_view(st, T),
            "prediction": {k: v for k, v in p.items() if k not in ("features",)},
            "features": p.get("features", {}),
            "trail": [{"t": self._local(t), "cur_dev_s": round(c, 1), "predicted_delay_s": round(pr, 1),
                       "p_late": round(pl, 3)} for t, c, pr, pl in st.pred_trail],
            "arrivals": [{"stop": st.schedule.stops[a.idx].address, "plan": self._local(st.schedule.stops[a.idx].plan_ts),
                          "fact": self._local(a.fact_ts), "delay_s": round(a.delay_s, 1)}
                         for a in sorted(st.arrivals.values(), key=lambda a: a.idx)[-12:] if a.detected]
            if st.schedule else [],
            "upcoming": self._upcoming_path(st, T),
            "stats": {"packets": st.packets, "arrivals_detected": st.arrivals_detected,
                      "arrivals_skipped": st.arrivals_skipped, "gps_fail_streak": st.gps_fail_streak},
        }

    def _upcoming_path(self, st: VehicleState, T: float) -> list[list[float]]:
        if st.schedule is None or not st.initialized:
            return []
        p = st.prediction or {}
        stops = st.schedule.stops
        end = min(len(stops) - 1, p.get("target_idx", st.next_idx + 8))
        path = [[st.lat, st.lon]] if st.lat is not None else []
        path += [[stops[i].lat, stops[i].lon] for i in range(st.next_idx, end + 1)]
        return path

    def _segment_levels(self) -> dict[str, str]:
        """Уровни риска участков сети: путь каждого ТС от текущего сегмента до целевой остановки."""
        levels: dict[str, str] = {}
        for st in self.vehicles.values():
            p = st.prediction
            if not p or p.get("status") != "ok" or p["level"] == "green" or st.schedule is None:
                continue
            stops = st.schedule.stops
            a = max(0, st.next_idx - 1)
            b = min(len(stops) - 1, p["target_idx"])
            for i in range(a, b):
                if stops[i].stop_key == stops[i + 1].stop_key:
                    continue
                sid = seg_id(stops[i].stop_key, stops[i + 1].stop_key)
                if sid in self.ref.segments and LEVEL_ORDER[p["level"]] > LEVEL_ORDER.get(levels.get(sid, "gray"), 0):
                    levels[sid] = p["level"]
        return levels

    def _build_snapshot(self, T: float, wall: float) -> None:
        vehicles = [self.vehicle_view(st, T) for st in self.vehicles.values()]
        vehicles.sort(key=lambda v: (-LEVEL_ORDER[v["level"]], -(v["predicted_delay_s"] or -1e9)))
        incidents = []
        for inc in self.incidents.values():
            st = next((v for v in self.vehicles.values() if v.tr_id == inc["tr_id"]), None)
            if st is not None:
                incidents.append(self._incident_view(inc, st))
        incidents.sort(key=lambda i: (i["acknowledged"], -LEVEL_ORDER.get(i["level"], 0), -(i["predicted_delay_s"] or 0)))
        self.segment_levels = self._segment_levels()

        routes = {}
        for v in vehicles:
            if not v["route_id"]:
                continue
            r = routes.setdefault(v["route_id"], {"id": v["route_id"], "vehicles": 0, "red": 0, "yellow": 0,
                                                  "green": 0, "level": "gray"})
            r["vehicles"] += 1
            if v["level"] in ("red", "yellow", "green"):
                r[v["level"]] += 1
                if LEVEL_ORDER[v["level"]] > LEVEL_ORDER[r["level"]]:
                    r["level"] = v["level"]

        counts = {lvl: sum(1 for v in vehicles if v["level"] == lvl) for lvl in ("red", "yellow", "green", "gray")}
        self.snapshot = {
            "type": "snapshot",
            "wall": wall,
            "data_time_utc": fmt_utc(T)[:19],
            "data_time_local": datetime.fromtimestamp(T, tz=self.tz).strftime("%d.%m.%Y %H:%M:%S"),
            "status": self.status(),
            "kpi": {
                "vehicles_total": len(vehicles),
                "scheduled": sum(1 for v in vehicles if v["scheduled"]),
                "stale": sum(1 for v in vehicles if v["stale"]),
                "no_target": sum(1 for v in vehicles if v["no_target"] and not v["stale"]),
                **counts,
                "incidents_open": len(incidents),
                "incidents_unacked": sum(1 for i in incidents if not i["acknowledged"]),
            },
            "vehicles": vehicles,
            "incidents": incidents,
            "resolved": list(self.resolved)[:15],
            "routes": sorted(routes.values(), key=lambda r: (-LEVEL_ORDER[r["level"]], r["id"])),
            "segments": self.segment_levels,
            "events": [{"time": datetime.fromtimestamp(e["wall"], tz=self.tz).strftime("%H:%M:%S"),
                        "level": e["level"], "text": e["text"]} for e in list(self.events)[:12]],
            "perf": self.perf_brief(),
        }

    def perf_brief(self) -> dict:
        return {
            "packets_per_s": round(self.packets.rate(), 2),
            "ml_p95_ms": self.ml.latency.summary()["p95"],
            "e2e_p95_ms": self.e2e_ms.summary()["p95"],
            "cycle_p95_ms": self.cycle_ms.summary()["p95"],
            "accuracy": self.accuracy(),
        }

    def accuracy(self) -> dict:
        n = len(self.acc_model)
        if not n:
            return {"n": 0, "mae_model_s": None, "mae_baseline_s": None}
        return {"n": n, "mae_model_s": round(sum(self.acc_model) / n, 1),
                "mae_baseline_s": round(sum(self.acc_base) / n, 1)}

    def metrics(self) -> dict:
        return {
            "ingest": {
                "packets_total": self.packets.total, "packets_per_s": round(self.packets.rate(), 2),
                "frames_total": self.frames_total, "handshakes": self.handshakes,
                "history_packets": self.history_packets, "unknown_units": sorted(self.unknown_units),
                "processing_us": self.ingest_us.summary(),
            },
            "prediction": {
                "cycles": self.cycles, "cycle_overruns": self.cycle_overruns,
                "cycle_ms": self.cycle_ms.summary(),
                "predictions_total": self.predictions_total,
                "fallback_predictions": self.fallback_predictions,
                "ml_request_ms": self.ml.latency.summary(),
                "e2e_packet_to_prediction_ms": self.e2e_ms.summary(),
            },
            "accuracy_online": self.accuracy(),
            "status": self.status(),
        }

    # ============================================================ pub/sub
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def _broadcast(self) -> None:
        for q in list(self.subscribers):
            # медленный клиент не копит очередь: держим только самый свежий снимок
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(self.snapshot)


def _r(v, nd: int = 0):
    if v is None:
        return None
    return round(float(v), nd) if nd else round(float(v))
