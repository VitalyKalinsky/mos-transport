"""Механизм «What-if»: моделирование управляющих решений и внештатных ситуаций.

Сценарий превращается в воздействия (:class:`app.eta.Overrides`) на ETA-движок. Результат — сравнение
«до/после» по затронутым ТС: ETA на ближайшие остановки, суммарное опоздание, число остановок
с опозданием > 2 мин, каскад на следующие рейсы («эффект домино»).

Это имитационная модель поверх текущего состояния сети, а не ML: эффектов управляющих воздействий
в разметке датасета нет, поэтому коэффициенты экспертные.

Сценарии (``type``):

* ``reserve_bus`` — выпуск резерва на линию за проблемным ТС;
* ``detour`` — объезд с закрытием участка и остановок;
* ``signal_priority`` — светофорный приоритет ОТ на участках;
* ``domino`` — купирование «эффекта домино» (укороченный рейс / пропуск остановок / резерв / удержание);
* ``skip_stops`` — пропуск остановок;
* ``breakdown`` — поломка ТС (стоит N минут);
* ``accident`` — ДТП на участке (падает пропускная способность);
* ``blockage`` — засор или затор на участке;
* ``traffic`` — пробки (балл 0–10) и автобусная полоса.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from . import routing
from .eta import DWELL_S, LAYOVER_GAP_S, EtaEngine, EtaResult, Overrides
from .geo import haversine_m
from .reference import seg_id
from .tracker import VehicleState

log = logging.getLogger("backend.whatif")

LATE_S = 120.0  # «опоздание» для подсчёта остановок — как target_class=late
SCENARIO_TYPES = {
    "reserve_bus": "Выпуск резерва",
    "detour": "Смена маршрута (объезд)",
    "signal_priority": "Светофорный приоритет",
    "domino": "Купирование эффекта домино",
    "skip_stops": "Пропуск остановок",
    "breakdown": "Поломка ТС",
    "accident": "ДТП на участке",
    "blockage": "Засор / затор на участке",
    "traffic": "Пробки и автобусная полоса",
}


@dataclass
class Plan:
    """Результат разбора сценариев: воздействия + геометрия и пояснения для карты."""
    ov: Overrides = field(default_factory=Overrides)
    units: set[int] = field(default_factory=set)
    reserve: list[dict] = field(default_factory=list)
    domino: list[dict] = field(default_factory=list)
    shapes: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def merge(a: Overrides | None, b: Overrides) -> Overrides:
    """Объединяет воздействия (живые применённые + моделируемые).

    Args:
        a: Уже действующие воздействия или ``None``.
        b: Новые воздействия.

    Returns:
        Overrides: Новый объект; ``a`` не изменяется. Множители перемножаются, объезды складываются,
        для остановок берётся худший фактор.
    """
    if a is None:
        return b
    m = copy.deepcopy(a)  # a — живое состояние движка, мутировать нельзя
    for k, v in b.segment_factor.items():
        m.segment_factor[k] = m.segment_factor.get(k, 1.0) * v
    for k, v in b.stop_factor.items():
        m.stop_factor[k] = max(m.stop_factor.get(k, 1.0), v)
    m.extra_delay.update(b.extra_delay)
    for u, s in b.skip_stops.items():
        m.skip_stops.setdefault(u, set()).update(s)
    m.dwell_factor.update(b.dwell_factor)
    for k, v in b.detour.items():
        m.detour[k] = m.detour.get(k, 0.0) + v
    m.hold_at.update(b.hold_at)
    m.closed_stops |= b.closed_stops
    for u, s in b.cut_segments.items():
        m.cut_segments.setdefault(u, set()).update(s)
    return m


class WhatIf:
    """Моделирование сценариев поверх живого состояния движка.

    Args:
        engine: Движок backend (ТС, ETA-движок, часы данных).
    """

    def __init__(self, engine) -> None:
        self.engine = engine
        self.eta: EtaEngine = engine.eta

    # ------------------------------------------------------------ helpers
    def _vehicle(self, unit_or_tr: int | None) -> VehicleState | None:
        """ТС по unit_id или tr_id (диспетчер знает tr_id, фидер — unit_id)."""
        if unit_or_tr is None:
            return None
        v = self.engine.vehicles.get(int(unit_or_tr))
        if v is None:
            v = next((x for x in self.engine.vehicles.values() if x.tr_id == int(unit_or_tr)), None)
        return v

    def _segments_ahead(self, st: VehicleState, n: int) -> list[tuple[int, str]]:
        """Ближайшие ``n`` перегонов впереди ТС (без межрейсовых)."""
        stops = st.schedule.stops
        j = max(1, st.next_idx)
        out = []
        for i in range(j, min(len(stops), j + n)):
            a, b = stops[i - 1], stops[i]
            if a.stop_key != b.stop_key and b.plan_ts - a.plan_ts < LAYOVER_GAP_S:
                out.append((i, seg_id(a.stop_key, b.stop_key)))
        return out

    def _segments_near(self, lat: float, lon: float, radius_m: float) -> list[str]:
        """Перегоны, середина которых в радиусе от точки (сценарий по точке на карте)."""
        res = []
        for sid, seg in self.engine.ref.segments.items():
            mid = seg["coords"][len(seg["coords"]) // 2]
            if haversine_m(lon, lat, mid[1], mid[0]) <= radius_m:
                res.append(sid)
        return res

    def _seg_geom(self, sid: str) -> list[list[float]]:
        seg = self.engine.ref.segments.get(sid)
        return seg["coords"] if seg else []

    # ------------------------------------------------------------ build
    async def build(self, scenarios: list[dict], T: float) -> Plan:
        """Переводит сценарии в воздействия на ETA-движок.

        Args:
            scenarios: ``[{type, params}]``.
            T: Текущее время данных.

        Returns:
            Plan: Воздействия, геометрия для карты, пояснения. Невыполнимые сценарии не бросают
            исключение, а попадают в ``notes``.
        """
        plan = Plan()
        for sc in scenarios:
            kind = sc.get("type")
            p = sc.get("params", {})
            st = self._vehicle(p.get("unit_id") or p.get("tr_id"))
            if st is not None:
                if st.schedule is None or not st.initialized:
                    plan.notes.append(f"ТС {p.get('unit_id') or p.get('tr_id')}: нет привязки к графику — сценарий пропущен")
                    continue
                plan.units.add(st.unit_id)
            ov = plan.ov
            if kind == "breakdown":
                minutes = float(p.get("minutes", 10))
                ov.extra_delay[st.unit_id] = (T, minutes * 60)
                plan.shapes.append({"kind": "incident", "lat": st.lat, "lon": st.lon,
                                    "label": f"Поломка ТС {st.tr_id}: {minutes:.0f} мин"})
            elif kind in ("accident", "blockage"):
                minutes = float(p.get("minutes", 20))
                cap = float(p.get("capacity", 0.35 if kind == "accident" else 0.6))
                sids = p.get("segment_ids") or ([s for _, s in self._segments_ahead(st, 2)] if st else [])
                if not sids and "lat" in p:
                    sids = self._segments_near(p["lat"], p["lon"], p.get("radius_m", 300))
                factor = 1.0 / max(0.1, cap)  # пропускная способность 35% → перегон ≈ в 2.9 раза дольше
                for sid in sids:
                    ov.segment_factor[sid] = ov.segment_factor.get(sid, 1.0) * factor
                    plan.shapes.append({"kind": "segment", "severity": "red", "coords": self._seg_geom(sid),
                                        "label": ("ДТП" if kind == "accident" else "Засор") + f" · {minutes:.0f} мин"})
                if kind == "accident" and st is not None and p.get("involved", True):
                    # ТС — участник ДТП: простой на половину длительности (экспертная оценка)
                    ov.extra_delay[st.unit_id] = (T, minutes * 60 * 0.5)
                plan.notes.append(f"{SCENARIO_TYPES[kind]}: {len(sids)} перегон(ов), пропускная способность {cap:.0%}")
            elif kind == "traffic":
                jam = float(p.get("jam", 6))
                lane = 1.0 if p.get("bus_lane") else 0.0
                sids = p.get("segment_ids") or []
                if not sids and st is not None:
                    sids = [s for _, s in self._segments_ahead(st, int(p.get("segments", 8)))]
                if not sids and "lat" in p:
                    sids = self._segments_near(p["lat"], p["lon"], p.get("radius_m", 1500))
                f = 1.0 + 0.12 * jam  # та же шкала, что в PUT /api/traffic
                if lane:
                    f = 1.0 + (f - 1.0) * (1.0 - 0.7 * lane)
                for sid in sids:
                    ov.segment_factor[sid] = ov.segment_factor.get(sid, 1.0) * f
                    plan.shapes.append({"kind": "segment", "severity": "yellow", "coords": self._seg_geom(sid),
                                        "label": f"Пробки {jam:.0f} б." + (" · выделенка" if lane else "")})
                plan.notes.append(f"Пробки {jam:.0f} баллов на {len(sids)} перегонах"
                                  + (", автобусная полоса снимает 70% влияния затора" if lane else ""))
            elif kind == "signal_priority":
                gain = float(p.get("gain", 0.15))
                sids = p.get("segment_ids") or ([s for _, s in self._segments_ahead(st, int(p.get("segments", 10)))] if st else [])
                for sid in sids:
                    ov.segment_factor[sid] = ov.segment_factor.get(sid, 1.0) * (1.0 - gain)
                    plan.shapes.append({"kind": "segment", "severity": "good", "coords": self._seg_geom(sid),
                                        "label": f"Приоритет на светофорах −{gain:.0%}"})
            elif kind == "skip_stops":
                n = int(p.get("count", 2))
                idxs = p.get("stops") or self._skippable(st, n)
                ov.skip_stops.setdefault(st.unit_id, set()).update(int(i) for i in idxs)
                for i in idxs:
                    s = st.schedule.stops[int(i)]
                    plan.shapes.append({"kind": "stop", "lat": s.lat, "lon": s.lon, "label": f"Пропуск: {s.address}"})
            elif kind == "detour":
                await self._detour(st, p, plan)
            elif kind == "reserve_bus":
                self._reserve(st, p, T, plan)
            elif kind == "domino":
                self._domino_action(st, p, T, plan)
            else:
                plan.notes.append(f"Неизвестный сценарий: {kind}")
        return plan

    def _skippable(self, st: VehicleState, n: int) -> list[int]:
        """Ближайшие промежуточные остановки: конечные и остановки у отстоя пропускать нельзя."""
        stops, res = st.schedule.stops, []
        for i in range(st.next_idx + 1, min(len(stops) - 1, st.next_idx + 12)):
            if stops[i].plan_ts - stops[i - 1].plan_ts < LAYOVER_GAP_S and \
                    stops[i + 1].plan_ts - stops[i].plan_ts < LAYOVER_GAP_S:
                res.append(i)
            if len(res) >= n:
                break
        return res

    async def _detour(self, st: VehicleState, p: dict, plan: Plan) -> None:
        """Объезд: закрытие участка, маршрут по дорогам в обход, добавочное время = удлинение / скорость."""
        stops = st.schedule.stops
        start = max(1, st.next_idx + int(p.get("offset", 0)))
        n = max(1, int(p.get("closed_stops", 1)))
        a_i, b_i = start - 1, min(len(stops) - 1, start + n)
        a, b = stops[a_i], stops[b_i]
        orig = sum(self.eta.ref.polyline(stops[i], stops[i + 1]).length for i in range(a_i, b_i))
        orig_time = sum(max(1.0, stops[i + 1].plan_ts - stops[i].plan_ts) for i in range(a_i, b_i))
        # OSRM не умеет исключать участки — строим маршрут через точку, смещённую вбок на side_m
        mid = self.eta.ref.polyline(stops[start - 1], stops[start]).point_at(
            self.eta.ref.polyline(stops[start - 1], stops[start]).length / 2)
        side = float(p.get("side_m", 450))
        dlat, dlon = (b.lat - a.lat), (b.lon - a.lon)
        norm = math.hypot(dlat, dlon * math.cos(math.radians(a.lat))) or 1e-9
        # нормаль к направлению a→b в градусах с учётом сжатия долготы
        via = [mid[0] - dlon * math.cos(math.radians(a.lat)) / norm * side / 111000,
               mid[1] + dlat / norm * side / (111000 * math.cos(math.radians(a.lat)))]
        geom, length = None, None
        try:
            r = await asyncio.to_thread(routing.route, [[a.lat, a.lon], via, [b.lat, b.lon]])
            geom, length = r["coords"], r["distance_m"]
        except routing.RoutingError as e:
            plan.notes.append(f"Роутер недоступен ({e}) — объезд оценён как +40% пути")
        if length is None:
            length = orig * 1.4
        speed = orig / orig_time if orig_time > 0 else 5.0
        extra = max(0.0, (length - orig) / max(2.0, speed))  # ≥ 2 м/с: иначе на коротких перегонах взрыв
        sid = seg_id(stops[a_i].stop_key, stops[a_i + 1].stop_key)
        plan.ov.detour[sid] = plan.ov.detour.get(sid, 0.0) + extra
        closed = [stops[i] for i in range(start, b_i)]
        plan.ov.skip_stops.setdefault(st.unit_id, set()).update(range(start, b_i))
        for i in range(a_i, b_i):
            plan.shapes.append({"kind": "segment", "severity": "closed", "coords": self._seg_geom(seg_id(stops[i].stop_key, stops[i + 1].stop_key)),
                                "label": "Участок закрыт"})
        if geom:
            plan.shapes.append({"kind": "detour", "coords": geom,
                                "label": f"Объезд: +{(length - orig) / 1000:.1f} км, ≈ +{extra / 60:.1f} мин"})
        plan.notes.append(f"Объезд ТС {st.tr_id}: закрыто остановок {len(closed)} "
                          f"({', '.join(s.address for s in closed[:3])}); путь {orig / 1000:.1f} → {length / 1000:.1f} км")

    def _reserve(self, st: VehicleState, p: dict, T: float, plan: Plan) -> None:
        """Резерв выходит на линию и идёт по графику проблемного ТС с первой остановки, до которой успевает.

        Пассажиры распределяются между ТС, поэтому стоянки проблемного ТС короче (``dwell_factor``).
        """
        dispatch = float(p.get("dispatch_min", 8)) * 60
        stops = st.schedule.stops
        start = next((i for i in range(st.next_idx, len(stops)) if stops[i].plan_ts >= T + dispatch), None)
        if start is None:
            plan.notes.append("Резерв не успевает выйти на линию до конца графика")
            return
        plan.ov.dwell_factor[st.unit_id] = float(p.get("dwell_factor", 0.6))
        plan.reserve.append({"unit_id": st.unit_id, "start_idx": start, "ready_ts": T + dispatch})
        s = stops[start]
        plan.shapes.append({"kind": "reserve", "lat": s.lat, "lon": s.lon,
                            "label": f"Резерв выходит на линию: {s.address} ({self.engine._local(max(s.plan_ts, T + dispatch))})"})

    def _domino_action(self, st: VehicleState, p: dict, T: float, plan: Plan) -> None:
        """Мера против каскада опозданий: ``short_turn`` | ``skip`` | ``reserve`` | ``hold``."""
        action = p.get("action", "short_turn")
        stops = st.schedule.stops
        plan.domino.append({"unit_id": st.unit_id, "action": action})
        if action == "skip":
            idx = self._skippable(st, int(p.get("count", 3)))
            plan.ov.skip_stops.setdefault(st.unit_id, set()).update(idx)
        elif action == "short_turn":
            # укороченный рейс: ТС не доезжает до конечной, разворачивается и начинает следующий рейс по графику
            end = next((i for i in range(st.next_idx + 1, len(stops))
                        if stops[i].plan_ts - stops[i - 1].plan_ts >= LAYOVER_GAP_S), None)
            if end is None:
                plan.notes.append("Укороченный рейс невозможен: нет следующего рейса в графике")
                return
            cut_from = st.next_idx + max(1, (end - st.next_idx) // 2)  # срезаем вторую половину остатка рейса
            plan.ov.skip_stops.setdefault(st.unit_id, set()).update(range(cut_from, end))
            plan.ov.cut_segments.setdefault(st.unit_id, set()).update(range(cut_from, end))
            plan.notes.append(f"Укороченный рейс: пропуск {end - cut_from} остановок до конечной, "
                              f"следующий рейс с {stops[end].address} по графику")
        elif action == "reserve":
            self._reserve(st, {"dispatch_min": p.get("dispatch_min", 8)}, T, plan)
        elif action == "hold":
            plan.notes.append("Удержание (регулирование интервала) применяется к ТС, идущему следом по маршруту")
            follower = self._follower(st)
            if follower is not None:
                plan.ov.hold_at[follower.unit_id] = (follower.next_idx, float(p.get("hold_s", 90)))
                plan.units.add(follower.unit_id)

    def _follower(self, st: VehicleState) -> VehicleState | None:
        """ТС, идущее следом: ≥ 3 общих остановки в ближайших 10 у обоих."""
        keys = {s.stop_key for s in st.schedule.stops[st.next_idx: st.next_idx + 10]}
        best = None
        for v in self.engine.vehicles.values():
            if v is st or v.schedule is None or not v.initialized:
                continue
            nxt = {s.stop_key for s in v.schedule.stops[v.next_idx: v.next_idx + 10]}
            if len(keys & nxt) >= 3:
                best = v
        return best

    # ------------------------------------------------------------ evaluate
    def _cascade(self, res: EtaResult) -> dict:
        """Каскад опоздания: остановки с опозданием > 2 мин, опаздывающие следующие рейсы, мин·ост."""
        late = [s for s in res.stops if s.delay_s > LATE_S and not s.skipped]
        trips, prev = 0, None
        for s in res.stops:
            if prev is not None and s.plan_ts - prev.plan_ts >= LAYOVER_GAP_S:
                trips += 1 if s.delay_s > LATE_S else 0  # первый пункт после отстоя = старт следующего рейса
            prev = s
        return {"late_stops": len(late), "later_trips_late": trips,
                "delay_min": round(sum(max(0.0, s.delay_s) for s in res.stops if not s.skipped) / 60, 1)}

    async def simulate(self, scenarios: list[dict], horizon: int = 12, include_all: bool = False) -> dict:
        """Моделирует сценарии и сравнивает «до/после».

        Args:
            scenarios: ``[{type, params}]``.
            horizon: Число остановок горизонта (для «домино» — 40, чтобы увидеть следующие рейсы).
            include_all: Включить ТС без изменений.

        Returns:
            dict: ``affected`` (по ТС), ``totals``, ``effect``, ``shapes``, ``notes``, ``recommendation``
            и служебный ``_overrides`` (для применения к живому прогнозу).
        """
        eng = self.engine
        T = eng.clock.now()
        plan = await self.build(scenarios, T)
        ov = merge(eng.live_overrides, plan.ov)  # «до» уже включает применённые ранее сценарии
        affected = []
        tot = {"before": {"delay_min": 0.0, "late_stops": 0}, "after": {"delay_min": 0.0, "late_stops": 0}}
        domino_units = {d["unit_id"] for d in plan.domino}
        for st in eng.vehicles.values():
            if st.schedule is None or not st.initialized or st.lat is None:
                continue
            k = 40 if st.unit_id in domino_units else horizon
            before = self.eta.predict(st, T, k, eng.live_overrides)
            after = self.eta.predict(st, T, k, ov)
            diff = max((abs(a.eta_ts - b.eta_ts) for a, b in zip(after.stops, before.stops)), default=0.0)
            if diff < 1.0 and st.unit_id not in plan.units and not include_all:
                continue
            reserve = next((r for r in plan.reserve if r["unit_id"] == st.unit_id), None)
            rows = []
            for b, a in zip(before.stops, after.stops):
                passenger = a.eta_ts
                if reserve and a.idx >= reserve["start_idx"]:
                    # пассажир уезжает первым пришедшим ТС: резерв идёт по графику
                    passenger = min(a.eta_ts, max(a.plan_ts, reserve["ready_ts"]))
                rows.append({"idx": a.idx, "address": a.address, "plan": eng._local(a.plan_ts),
                             "eta_before": eng._local(b.eta_ts), "eta_after": eng._local(passenger),
                             "delay_before_s": round(b.delay_s), "delay_after_s": round(passenger - a.plan_ts),
                             "skipped": a.skipped})
            cb = self._cascade(before)
            # каскад «после» — по времени для пассажира (с резервом), копия, чтобы не портить after
            after_for_cascade = copy.copy(after)
            after_for_cascade.stops = [copy.copy(s) for s in after.stops]
            for s, r in zip(after_for_cascade.stops, rows):
                s.eta_ts = s.plan_ts + r["delay_after_s"]
            ca = self._cascade(after_for_cascade)
            for key, c in (("before", cb), ("after", ca)):
                tot[key]["delay_min"] += c["delay_min"]
                tot[key]["late_stops"] += c["late_stops"]
            affected.append({"unit_id": st.unit_id, "tr_id": st.tr_id,
                             "route": (eng.route_names.get(st.tr_id) or {}).get("ref") or st.schedule.route_id,
                             "before": cb, "after": ca, "stops": rows})
        for key in tot:
            tot[key]["delay_min"] = round(tot[key]["delay_min"], 1)
        affected.sort(key=lambda a: -(a["before"]["delay_min"] - a["after"]["delay_min"]))
        return {
            "T": eng._local(T), "scenarios": scenarios, "affected": affected, "totals": tot,
            "effect": {"delay_min_saved": round(tot["before"]["delay_min"] - tot["after"]["delay_min"], 1),
                       "late_stops_delta": tot["after"]["late_stops"] - tot["before"]["late_stops"]},
            "shapes": plan.shapes, "notes": plan.notes,
            "recommendation": self._recommend(scenarios, tot),
            "_overrides": plan.ov,
        }

    @staticmethod
    def _recommend(scenarios: list[dict], tot: dict) -> str:
        """Текстовая рекомендация по суммарному эффекту (порог ±0.5 мин·ост. — ниже шума модели)."""
        saved = tot["before"]["delay_min"] - tot["after"]["delay_min"]
        names = ", ".join(SCENARIO_TYPES.get(s.get("type"), s.get("type", "")) for s in scenarios)
        if saved > 0.5:
            return (f"«{names}»: суммарное опоздание на остановках горизонта снижается на {saved:.1f} мин·ост., остановок с опозданием "
                    f">2 мин: {tot['before']['late_stops']} → {tot['after']['late_stops']}. Рекомендуется к применению.")
        if saved < -0.5:
            return (f"«{names}»: суммарное опоздание на остановках горизонта растёт на {-saved:.1f} мин·ост., остановок с опозданием "
                    f">2 мин: {tot['before']['late_stops']} → {tot['after']['late_stops']}. "
                    "Требуются компенсирующие меры (резерв, приоритет, регулирование интервалов).")
        return f"«{names}»: существенного влияния на график не ожидается."
