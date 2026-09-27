"""ETA-движок: прогноз прибытия на ближайшие остановки (не ML: кинематика + график).

Для каждого ТС:

* положение проецируется на геометрию перегона по дорогам → остаток пути до следующей остановки;
* время перегона i→i+1 = плановое × темп ТС (факт/план по последним перегонам) × коэффициент
  трафика участка (пробки, автобусная полоса, what-if);
* межрейсовый отстой на конечной поглощает опоздание: ТС не уходит раньше плана;
* при обрыве связи положение восстанавливается счислением пути (dead reckoning) по геометрии
  маршрута с последним темпом: ETA строятся «вслепую» с расширяющимся интервалом.

Сценарии what-if передаются объектом :class:`Overrides`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geo import haversine_m
from .reference import Reference, seg_id
from .tracker import VehicleState

LAYOVER_GAP_S = 360          # плановый разрыв ≥ 6 мин между остановками — отстой/межрейс
MIN_TURNAROUND_S = 120       # минимальный отстой на конечной
DEFAULT_SPEED_MPS = 5.0      # 18 км/ч — если плановое время перегона нулевое или это межрейс
DWELL_S = 20.0               # типичная стоянка на промежуточной остановке (для пропуска остановок)
FRESH_S = 60.0               # телеметрия моложе — положение «живое», иначе dead reckoning
import os as _os
EARLY_ALLOW_S = float(_os.getenv("ETA_EARLY_ALLOW_S", "0"))    # на сколько ТС может прийти раньше плана
# затухание отклонения к типичному уровню с горизонтом (подобрано на train, scripts/validate_eta.py)
DECAY_TAU_MIN = float(_os.getenv("ETA_DECAY_TAU_MIN", "10"))
PRIOR_DELAY_S = float(_os.getenv("ETA_PRIOR_DELAY_S", "30"))


@dataclass
class Overrides:
    """Воздействия what-if и живые факторы трафика, применяемые к прогнозу ETA."""
    segment_factor: dict[str, float] = field(default_factory=dict)   # seg_id → множитель времени перегона
    stop_factor: dict[str, float] = field(default_factory=dict)      # stop_key → множитель примыкающих перегонов
    extra_delay: dict[int, tuple[float, float]] = field(default_factory=dict)  # unit → (с момента, сек простоя)
    skip_stops: dict[int, set[int]] = field(default_factory=dict)    # unit → индексы пропускаемых остановок
    dwell_factor: dict[int, float] = field(default_factory=dict)     # unit → множитель стоянок (резерв разгружает)
    detour: dict[str, float] = field(default_factory=dict)           # seg_id → доп. секунды на объезд
    hold_at: dict[int, tuple[int, float]] = field(default_factory=dict)  # unit → (индекс остановки, удержание, с)
    closed_stops: set[str] = field(default_factory=set)              # stop_key, не обслуживаемые (объезд)
    cut_segments: dict[int, set[int]] = field(default_factory=dict)  # unit → i: перегон (i-1→i) не проезжается (укороченный рейс)


@dataclass
class StopEta:
    """Прогноз прибытия на одну остановку.

    Attributes:
        idx: Индекс остановки в нитке графика.
        stop_id: tt_action_item_id.
        stop_key: Физическая остановка.
        address: Адрес для диспетчера.
        plan_ts: Плановое время, unix-с.
        eta_ts: Прогнозное время, unix-с.
        skipped: Остановка пропускается (what-if).
        sigma_s: Оценка неопределённости, с.
    """

    idx: int
    stop_id: int
    stop_key: str
    address: str
    plan_ts: float
    eta_ts: float
    skipped: bool = False
    sigma_s: float = 0.0

    @property
    def delay_s(self) -> float:
        """float: Прогнозное отклонение от плана, с (+ — опоздание)."""
        return self.eta_ts - self.plan_ts


@dataclass
class EtaResult:
    """ETA по ТС на момент T.

    Attributes:
        mode: ``live`` | ``dead_reckoning`` (нет связи > 60 с) | ``no_data``.
        est_lat, est_lon: Расчётное положение (при ``dead_reckoning``) либо последнее известное.
        next_idx: Индекс следующей остановки.
        pace: Темп ТС (факт/план).
        stops: Прогнозы на ближайшие остановки.
    """

    unit_id: int
    T: float
    mode: str
    est_lat: float | None
    est_lon: float | None
    next_idx: int
    pace: float
    stops: list[StopEta]


class EtaEngine:
    """Расчёт ETA для всех ТС + живой индекс загруженности перегонов.

    Args:
        ref: Справочники (расписание и геометрия перегонов).
    """

    def __init__(self, ref: Reference) -> None:
        self.ref = ref
        # загруженность перегонов: seg_id → (множитель, unit_id наблюдателя, время данных)
        self.traffic: dict[str, tuple[float, int, float]] = {}
        # внешний/ручной индекс пробок: seg_id → множитель (PUT /api/traffic, интеграции)
        self.manual_traffic: dict[str, float] = {}
        # автобусные полосы: seg_id → доля перегона с выделенной полосой (0..1)
        self.bus_lanes: dict[str, float] = {}

    # ------------------------------------------------------------- helpers
    def _seg_time(self, st: VehicleState, i: int, pace: float, ov: Overrides | None) -> float:
        """Время перегона i → i+1 с учётом темпа, трафика и воздействий, с."""
        stops = st.schedule.stops
        a, b = stops[i], stops[i + 1]
        plan_dt = b.plan_ts - a.plan_ts
        pl = self.ref.polyline(a, b)
        if plan_dt <= 0 or plan_dt >= LAYOVER_GAP_S:
            # план не отражает езду (одинаковое время или отстой внутри) — берём длину / типичную скорость
            base = pl.length / DEFAULT_SPEED_MPS
        else:
            base = plan_dt
        sid = seg_id(a.stop_key, b.stop_key)
        f = pace * self.traffic_factor(sid, st.unit_id, a.plan_ts)
        extra = 0.0
        if ov is not None:
            f *= ov.segment_factor.get(sid, 1.0)
            f *= max(ov.stop_factor.get(a.stop_key, 1.0), ov.stop_factor.get(b.stop_key, 1.0))
            extra += ov.detour.get(sid, 0.0)
        return base * f + extra

    def traffic_factor(self, sid: str, unit_id: int, ts: float, jam: float | None = None) -> float:
        """Множитель времени перегона от пробок с учётом автобусной полосы.

        Args:
            sid: Сегмент.
            unit_id: ТС, для которого считаем: его собственные проезды не учитываются
                (своё отставание уже сидит в темпе).
            ts: Время данных; наблюдения старше часа игнорируются.
            jam: Явный множитель пробки (what-if); иначе — внешний индекс ``manual_traffic``.

        Returns:
            float: Множитель ≥ ~0.5; выделенная полоса снимает до 70% влияния затора.
        """
        f = 1.0
        obs = self.traffic.get(sid)
        if obs is not None and obs[1] != unit_id and abs(ts - obs[2]) < 3600:
            f *= 1.0 + 0.5 * (obs[0] - 1.0)  # половина эффекта: чужой проезд — косвенный сигнал
        f *= jam if jam is not None else self.manual_traffic.get(sid, 1.0)
        lane = self.bus_lanes.get(sid, 0.0)
        if f > 1.0 and lane > 0:
            f = 1.0 + (f - 1.0) * (1.0 - 0.7 * lane)
        return f

    def pace(self, st: VehicleState) -> float:
        """Темп ТС: медиана (факт/план) по последним перегонам.

        Args:
            st: Состояние ТС.

        Returns:
            float: >1 — едет медленнее графика; обрезан до [0.6, 2.5] от выбросов детектора прибытий.
        """
        r = sorted(st.pace_samples)
        if len(r) < 2:
            return 1.0
        return max(0.6, min(2.5, r[len(r) // 2]))

    def locate(self, st: VehicleState, T: float) -> int:
        """Индекс следующей остановки по положению ТС на геометрии маршрута.

        Указатель трекера (``next_idx``) надёжен после детекции прибытий; при старте потока или после
        пропусков детектора он может отставать. Поэтому выбирается перегон (j-1 → j) рядом с указателем,
        минимизирующий расстояние до геометрии + штраф за расхождение с планом по времени.

        Args:
            st: Состояние ТС.
            T: Момент прогноза (время данных).

        Returns:
            int: Индекс следующей остановки.
        """
        stops = st.schedule.stops
        n = len(stops)
        j0 = min(st.next_idx, n - 1)
        # свежая детекция прибытия (< 10 мин) надёжнее геометрии — доверяем трекеру
        if st.lat is None or (st.last_arrival is not None and st.last_arrival.detected
                              and st.last_ts - st.last_arrival.fact_ts < 600):
            return j0
        best, best_cost = j0, float("inf")
        for j in range(max(1, j0), min(n, j0 + 25)):
            a, b = stops[j - 1], stops[j]
            if b.plan_ts - a.plan_ts >= LAYOVER_GAP_S:
                continue
            _, off = self.ref.polyline(a, b).project(st.lat, st.lon)
            # 0.5 м за секунду расхождения с планом разводит проходы одной улицы в разных рейсах
            cost = off + 0.5 * abs(b.plan_ts - T) + (0 if j >= j0 else 1e6)
            if cost < best_cost:
                best, best_cost = j, cost
        return best if best_cost < 400 else j0

    # ------------------------------------------------------------- predict
    def predict(self, st: VehicleState, T: float, k: int = 8, ov: Overrides | None = None) -> EtaResult:
        """ETA на ``k`` ближайших остановок.

        Args:
            st: Состояние ТС.
            T: Момент прогноза (время данных), unix-с.
            k: Число остановок.
            ov: Воздействия what-if; ``None`` — базовый прогноз.

        Returns:
            EtaResult: Режим, расчётное положение и прогнозы по остановкам
            (``mode="no_data"`` без расписания или координат).
        """
        base_kin: dict[int, float] = {}
        if ov is not None:
            # кинематика без воздействий — чтобы эффект сценария сохранялся на всём горизонте
            base = self._predict(st, T, k, None, raw=True)
            base_kin = {e.idx: e.eta_ts - e.plan_ts for e in base.stops}
        return self._predict(st, T, k, ov, base_kin=base_kin)

    def _predict(self, st: VehicleState, T: float, k: int, ov: Overrides | None,
                 raw: bool = False, base_kin: dict[int, float] | None = None) -> EtaResult:
        sch = st.schedule
        if sch is None or not st.initialized or st.lat is None:
            return EtaResult(st.unit_id, T, "no_data", st.lat, st.lon, st.next_idx, 1.0, [])
        stops = sch.stops
        n = len(stops)
        j = self.locate(st, T)
        pace = self.pace(st)
        skip = ov.skip_stops.get(st.unit_id, set()) if ov else set()
        dwell_f = ov.dwell_factor.get(st.unit_id, 1.0) if ov else 1.0

        # --- исходная точка: живое положение или счисление пути
        age = max(0.0, T - st.pos_ts)
        mode = "live" if age <= FRESH_S else "dead_reckoning"
        t = max(T, st.pos_ts)
        if j == 0:
            rem = haversine_m(st.lon, st.lat, stops[0].lon, stops[0].lat)
            seg_t = rem / DEFAULT_SPEED_MPS
            frac = 1.0
        else:
            pl = self.ref.polyline(stops[j - 1], stops[j])
            s_along, _off = pl.project(st.lat, st.lon)
            frac = 1.0 - (s_along / pl.length if pl.length > 0 else 1.0)
            seg_t = self._seg_time(st, j - 1, pace, ov)
        # время, оставшееся до остановки j от момента последней точки
        remaining = frac * seg_t
        standing = st.standing_since is not None and st.speed < 3
        if mode == "live" and standing:
            # стоит (светофор/затор): часть накопленного простоя — признак замедления, но не больше минуты
            remaining += min(60.0, st.dwell_s(T) * 0.25)
        est_lat, est_lon = st.lat, st.lon
        if mode == "dead_reckoning":
            # «проезжаем» вперёд по графу на время, прошедшее с последней точки
            elapsed = T - st.pos_ts
            idx, left = j, remaining
            while elapsed > left and idx < n - 1:
                elapsed -= left
                idx += 1
                left = self._seg_time(st, idx - 1, pace, ov)
            if elapsed <= left and idx >= 1:
                pl = self.ref.polyline(stops[idx - 1], stops[idx])
                seg_total = self._seg_time(st, idx - 1, pace, ov) or 1.0
                pos = pl.length * (1 - (left - elapsed) / seg_total)
                est_lat, est_lon = pl.point_at(pos)
            j, remaining, t = idx, max(0.0, left - elapsed), T

        # внештатный простой (поломка/ДТП): ТС стоит extra секунд начиная с момента since
        if ov and st.unit_id in ov.extra_delay:
            since, extra = ov.extra_delay[st.unit_id]
            remaining += max(0.0, since + extra - max(t, since)) if t < since + extra else 0.0

        out: list[StopEta] = []
        eta = t + remaining
        hold = ov.hold_at.get(st.unit_id) if ov else None
        horizon_min = 0.0
        for i in range(j, min(n, j + k)):
            s = stops[i]
            skipped = i in skip or (ov is not None and s.stop_key in ov.closed_stops)
            if i > j:
                prev = stops[i - 1]
                plan_gap = s.plan_ts - prev.plan_ts
                travel = self._seg_time(st, i - 1, pace, ov)
                if ov is not None and i in ov.cut_segments.get(st.unit_id, ()):
                    travel = 0.0                                   # укороченный рейс: участок не проезжается
                if plan_gap >= LAYOVER_GAP_S:
                    # отстой на конечной: не раньше плана, но не меньше минимального разворота
                    eta = max(eta + MIN_TURNAROUND_S + travel, s.plan_ts)
                else:
                    dwell_adj = (dwell_f - 1.0) * DWELL_S
                    if out and out[-1].skipped:
                        dwell_adj -= DWELL_S                 # на пропущенной остановке не стоим
                    eta += max(5.0, travel + dwell_adj)      # ≥ 5 с: перегон не бывает мгновенным
            # ТС не уходит с остановки раньше графика (в данных медиана факт−план = +23 с)
            eta = max(eta, s.plan_ts - EARLY_ALLOW_S)
            if i == j and j > 0 and s.plan_ts - stops[j - 1].plan_ts >= LAYOVER_GAP_S:
                eta = max(eta, s.plan_ts)                      # отстой на конечной: отправление по графику
            if hold and hold[0] == i:
                eta += hold[1]                                 # удержание ТС на остановке (регулирование интервала)
            if raw:
                out.append(StopEta(i, s.item_id, s.stop_key, s.address, s.plan_ts, eta, skipped, 0.0))
                continue
            horizon_min = max(0.0, (eta - T) / 60)
            # кинематика надёжна на ближнем горизонте; дальше отклонение «растворяется»
            # (отстои, нагон графика водителем) → экспоненциальное затухание к типичному уровню
            w = math.exp(-horizon_min / DECAY_TAU_MIN) if DECAY_TAU_MIN > 0 else 1.0
            d_kin = eta - s.plan_ts
            eta_out = s.plan_ts + w * d_kin + (1 - w) * PRIOR_DELAY_S
            if ov is not None:
                # эффект what-if — сдвиг относительно базовой кинематики, он не должен «затухать»
                eta_out += (1 - w) * (d_kin - base_kin.get(i, d_kin)) if base_kin else 0.0
            # неопределённость растёт с горизонтом и с возрастом последней точки
            sigma = 20 + 6 * horizon_min + (0.15 * age if mode == "dead_reckoning" else 0)
            out.append(StopEta(i, s.item_id, s.stop_key, s.address, s.plan_ts, eta_out, skipped, sigma))
        return EtaResult(st.unit_id, T, mode, est_lat, est_lon, j, pace, out)

    # ------------------------------------------------------------- traffic
    def update_traffic(self, vehicles: list[VehicleState]) -> None:
        """Обновляет живой индекс загруженности перегонов по фактическим проездам.

        Args:
            vehicles: ТС, у которых накопились наблюдения проездов (факт/план по перегонам).
        """
        for st in vehicles:
            for sid, ratio, ts in st.pop_segment_observations():
                old = self.traffic.get(sid)
                r = max(0.5, min(3.0, ratio))  # отсечка выбросов детектора прибытий
                # EMA 0.3: один медленный проезд не перекрашивает участок, серия — да
                self.traffic[sid] = (r if old is None else 0.7 * old[0] + 0.3 * r, st.unit_id, ts)

    # ------------------------------------------------------------- projection
    def position_at(self, st: VehicleState, res: EtaResult, t: float) -> list[float] | None:
        """Прогнозное положение ТС в момент ``t`` (будущая часть таймлайна).

        Args:
            st: Состояние ТС.
            res: Результат :meth:`predict`.
            t: Момент (время данных), unix-с.

        Returns:
            list[float] | None: ``[lat, lon]`` на геометрии маршрута; ``None`` — за горизонтом ETA.
        """
        if not res.stops or st.schedule is None:
            return None
        stops = st.schedule.stops
        prev_t = res.T
        prev_pt = [res.est_lat, res.est_lon] if res.est_lat is not None else [st.lat, st.lon]
        for e in res.stops:
            if t <= e.eta_ts:
                u = 0.0 if e.eta_ts <= prev_t else (t - prev_t) / (e.eta_ts - prev_t)
                u = max(0.0, min(1.0, u))
                if e.idx >= 1:
                    pl = self.ref.polyline(stops[e.idx - 1], stops[e.idx])
                    s0 = pl.project(prev_pt[0], prev_pt[1])[0] if e.idx == res.next_idx else 0.0
                    return pl.point_at(s0 + u * (pl.length - s0))
                return [prev_pt[0] + u * (stops[e.idx].lat - prev_pt[0]), prev_pt[1] + u * (stops[e.idx].lon - prev_pt[1])]
            prev_t, prev_pt = e.eta_ts, [stops[e.idx].lat, stops[e.idx].lon]
        return None
