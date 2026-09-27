"""Состояние ТС: наложение телеметрии на нитку графика (map matching) и производные признаки.

* Фактическое прибытие — геозона ``stop_radius_m`` вокруг остановки. Проверяется и точка, и отрезок
  трека между пакетами. Сопоставление идёт по порядку расписания в окне lookahead, поэтому кольцевые
  маршруты и остановки встречного направления не дают ложных срабатываний.
* Текущее отклонение ``cur_dev_s`` — задержка на последней остановке с планом ≤ T (как в разметке).
  Если ТС до неё ещё не доехало, берётся последнее детектированное отклонение (``CUR_DEV_MODE``).
* Производные признаки: средняя и плановая скорость на сегменте, время простоя, возраст телеметрии,
  серия сбоев GPS, тренд отклонения.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Settings
from .geo import haversine_m
from .reference import VehicleSchedule


def fmt_utc(ts: float) -> str:
    """unix-с → строка времени в формате traffic.csv (ML-ядро парсит именно его)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


@dataclass(slots=True)
class Arrival:
    """Прибытие на остановку.

    Attributes:
        idx: Индекс остановки в нитке графика.
        fact_ts: Фактическое (или оценочное) время, unix-с.
        delay_s: Отклонение от плана, с.
        detected: ``False`` — остановка пропущена детектором (нет GPS в геозоне), отклонение перенесено.
    """

    idx: int
    fact_ts: float
    delay_s: float
    detected: bool = True


@dataclass
class VehicleState:
    """Живое состояние одного ТС (по одному на терминал).

    Args:
        unit_id: Терминал (NDTP peerAddress).
        tr_id: ТС; ``None`` — терминал без наряда.
        schedule: Нитка графика; ``None`` — ТС без расписания (серое на карте).
        cfg: Настройки.
    """

    unit_id: int
    tr_id: int | None
    schedule: VehicleSchedule | None
    cfg: Settings

    # последняя телеметрия
    last_ts: float = 0.0                 # event_time последнего пакета (время данных)
    last_rx_wall: float = 0.0            # время приёма (wall-clock) — для контроля связи
    lon: float | None = None
    lat: float | None = None
    pos_ts: float = 0.0                  # время последней валидной координаты (для dead reckoning)
    speed: float = 0.0
    heading: float = 0.0
    location_valid: bool = False
    gps_fail_streak: int = 0
    packets: int = 0

    history: deque = field(default_factory=lambda: deque(maxlen=40))   # сырые строки для ML (с запасом к ml_history_rows)

    # нитка графика
    next_idx: int = 0
    initialized: bool = False
    arrivals: dict[int, Arrival] = field(default_factory=dict)
    last_arrival: Arrival | None = None
    arrivals_detected: int = 0
    arrivals_skipped: int = 0

    # сегмент (от последней пройденной остановки)
    seg_start_ts: float | None = None
    seg_dist_m: float = 0.0
    standing_since: float | None = None

    # темп и наблюдения перегонов (для ETA-движка и индекса загруженности)
    pace_samples: deque = field(default_factory=lambda: deque(maxlen=6))
    seg_observations: list = field(default_factory=list)
    eta: object | None = None

    # прогноз
    prediction: dict | None = None
    pred_trail: deque = field(default_factory=lambda: deque(maxlen=180))
    pending_eval: dict[int, list[tuple[float, float, float]]] = field(default_factory=dict)  # для онлайн-MAE по факту прибытия
    dirty: bool = True  # пришли новые данные → ТС попадёт в ближайший батч ML

    # ------------------------------------------------------------------ ingest
    def reset_schedule_state(self) -> None:
        """Сбрасывает привязку к нитке графика (перезапуск реплея)."""
        self.next_idx = 0
        self.initialized = False
        self.arrivals.clear()
        self.last_arrival = None
        self.seg_start_ts = None
        self.seg_dist_m = 0.0
        self.standing_since = None
        self.pending_eval.clear()
        self.pred_trail.clear()
        self.pace_samples.clear()
        self.seg_observations.clear()
        self.history.clear()
        self.prediction = None

    def ingest(self, ts: float, lon: float | None, lat: float | None, valid: bool,
               speed: float, heading: float, alt: float, is_hist: bool,
               rx_wall: float | None = None) -> list[Arrival]:
        """Применяет пакет телеметрии.

        Args:
            ts: Время пакета (время данных), unix-с.
            lon: Долгота или ``None``.
            lat: Широта или ``None``.
            valid: Флаг достоверности координат (бит 7 Nav00).
            speed: Скорость, км/ч.
            heading: Курс, градусы.
            alt: Высота, м.
            is_hist: Пакет из «чёрного ящика» (передан после восстановления связи).
            rx_wall: Время приёма (wall-clock).

        Returns:
            list[Arrival]: Новые детектированные прибытия (0 или 1).
        """
        rx_wall = rx_wall or time.time()
        # скачок времени назад > 1 ч (перезапуск реплея/эмулятора) → начинаем нитку заново
        if self.last_ts and ts < self.last_ts - 3600:
            self.reset_schedule_state()
            self.last_ts = 0.0
        self.packets += 1
        self.last_rx_wall = rx_wall
        self.dirty = True

        has_pos = valid and lon is not None and lat is not None
        # в ML уходит и невалидный пакет: признак gps_failure считается ML-ядром по location_valid
        self.history.append({
            "tr_id": self.tr_id if self.tr_id is not None else 0,
            "unit_id": self.unit_id,
            "event_time": fmt_utc(ts),
            "location_valid": bool(has_pos),
            "lon": lon if has_pos else None,
            "lat": lat if has_pos else None,
            "alt": alt if has_pos else None,
            "speed": speed if has_pos else None,
            "heading": heading if has_pos else None,
            "is_hist_data": is_hist,
        })

        if ts < self.last_ts:            # опоздавший (исторический) пакет — только в историю, положение не откатываем
            return []
        prev_lon, prev_lat, prev_pos_ts = self.lon, self.lat, self.pos_ts
        self.last_ts = ts

        if not has_pos:
            self.gps_fail_streak += 1
            self.location_valid = False
            return []  # положение остаётся последним валидным

        self.gps_fail_streak = 0
        self.location_valid = True
        self.lon, self.lat, self.pos_ts = lon, lat, ts
        self.speed, self.heading = speed, heading

        # простой: считаем от первого пакета со скоростью ниже порога
        if speed < self.cfg.standing_speed_kmh:
            if self.standing_since is None:
                self.standing_since = ts
        else:
            self.standing_since = None

        # пройденный путь на сегменте (отсекаем GPS-скачки)
        prev = None
        if prev_lon is not None and prev_lat is not None:
            d = haversine_m(prev_lon, prev_lat, lon, lat)
            dt = max(1.0, ts - prev_pos_ts)
            if d / dt < 40.0 and dt <= 120:   # < 144 км/ч и без длинного разрыва связи
                self.seg_dist_m += d
                prev = (prev_lon, prev_lat, prev_pos_ts)

        return self._match_schedule(ts, lon, lat, prev)

    # ------------------------------------------------------ schedule matching
    def _match_schedule(self, ts: float, lon: float, lat: float,
                        prev: tuple[float, float, float] | None = None) -> list[Arrival]:
        """Ищет прохождение ближайших по графику остановок; первая найденная — прибытие."""
        sch = self.schedule
        if sch is None or not sch.stops:
            return []
        stops = sch.stops
        n = len(stops)
        if not self.initialized:
            # первая привязка (старт потока посреди дня): часть нитки, которую ТС ещё может проходить
            self.next_idx = max(0, sch.last_planned_before(ts - self.cfg.max_late_s) + 1)
            self.initialized = True
            self.seg_start_ts = ts

        # остановки, которые давно должны были быть пройдены, считаем пропущенными детектором
        while self.next_idx < n and stops[self.next_idx].plan_ts < ts - self.cfg.max_late_s:
            self._skip(self.next_idx)
            self.next_idx += 1

        hit: tuple[int, float] | None = None
        for j in range(self.next_idx, min(n, self.next_idx + self.cfg.stop_lookahead)):
            s = stops[j]
            if s.plan_ts - ts > self.cfg.max_early_s:  # дальше по графику — слишком рано, это следующий круг
                break
            hit_ts = self._passage_time(s.lon, s.lat, lon, lat, ts, prev)
            if hit_ts is not None:
                hit = (j, hit_ts)
                break
        if hit is None:
            return []

        j, hit_ts = hit
        for k in range(self.next_idx, j):  # перескочили остановки — GPS не попал в их геозоны
            self._skip(k)
        arr = Arrival(j, hit_ts, hit_ts - stops[j].plan_ts, True)
        self._observe_pace(j, hit_ts)
        self.arrivals[j] = arr
        self.last_arrival = arr
        self.arrivals_detected += 1
        self.next_idx = j + 1
        self.seg_start_ts = ts
        self.seg_dist_m = 0.0
        return [arr]

    def _observe_pace(self, j: int, fact_ts: float) -> None:
        """Темп ТС: факт/план по последнему детектированному перегону (без межрейсовых отстоев)."""
        prev = self.last_arrival
        if prev is None or not prev.detected or not (0 < j - prev.idx <= 3):
            return
        stops = self.schedule.stops
        plan_dt = stops[j].plan_ts - stops[prev.idx].plan_ts
        fact_dt = fact_ts - prev.fact_ts
        # < 30 с — шум детектора даёт огромные отношения; ≥ 6 мин — внутри отстой
        if 30 <= plan_dt < 360 and fact_dt > 0:
            ratio = fact_dt / plan_dt
            self.pace_samples.append(ratio)
            if j - prev.idx == 1 and stops[prev.idx].stop_key != stops[j].stop_key:
                self.seg_observations.append((f"{stops[prev.idx].stop_key}>{stops[j].stop_key}", ratio, fact_ts))

    def pop_segment_observations(self) -> list:
        """Забирает накопленные наблюдения проездов (seg_id, факт/план, время) для индекса загруженности."""
        obs, self.seg_observations = self.seg_observations, []
        return obs

    def _passage_time(self, slon: float, slat: float, lon: float, lat: float, ts: float,
                      prev: tuple[float, float, float] | None) -> float | None:
        """Время прохождения геозоны остановки.

        GPS приходит раз в 10–15 с (60–80 м пути), и одиночная точка часто «проскакивает» геозону 45 м.
        Поэтому проверяется отрезок трека prev→cur: если он проходит в пределах радиуса,
        берётся момент ближайшего подхода (линейная интерполяция по времени).

        Returns:
            float | None: Время прохождения, unix-с; ``None`` — геозона не пересечена.
        """
        r = self.cfg.stop_radius_m
        if prev is None:
            return ts if haversine_m(lon, lat, slon, slat) <= r else None
        plon, plat, pts = prev
        # локальная равнопромежуточная проекция (метры) относительно остановки
        kx = 111320.0 * math.cos(math.radians(slat))
        ky = 110540.0
        ax, ay = (plon - slon) * kx, (plat - slat) * ky
        bx, by = (lon - slon) * kx, (lat - slat) * ky
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        u = 1.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
        cx, cy = ax + u * dx, ay + u * dy
        if cx * cx + cy * cy > r * r:
            return None
        return pts + u * (ts - pts)

    def _skip(self, k: int) -> None:
        if k not in self.arrivals:
            self.arrivals_skipped += 1
            # оценка: переносим последнее известное отклонение (помечено detected=False)
            last = self.last_arrival.delay_s if self.last_arrival else 0.0
            self.arrivals[k] = Arrival(k, self.schedule.stops[k].plan_ts + last, last, False)

    # --------------------------------------------------------- derived features
    def current_deviation(self, T: float) -> tuple[float, str]:
        """Текущее отклонение ``cur_dev_s`` на момент T.

        Args:
            T: Момент прогноза (время данных).

        Returns:
            tuple[float, str]: Отклонение, с, и способ: ``fact``, ``carried`` (перенесено через
            пропуск детектора), ``last_detected``, ``lower_bound``, ``before_start``, ``no_schedule``.
        """
        sch = self.schedule
        if sch is None:
            return 0.0, "no_schedule"
        k = sch.last_planned_before(T)
        if k < 0:
            return 0.0, "before_start"
        arr = self.arrivals.get(k)
        if arr is not None and arr.fact_ts <= T:
            return float(arr.delay_s), ("fact" if arr.detected else "carried")
        # ТС ещё не прибыло на остановку с планом ≤ T. В разметке здесь факт после T — онлайн он неизвестен.
        # (ETA по остаточному пути проверялась офлайн — точность хуже, см. scripts/validate_matching.py)
        if not self.initialized:
            return 0.0, "before_start"
        last = self.last_arrival.delay_s if self.last_arrival else 0.0
        if self.cfg.cur_dev_mode == "last_detected":
            return float(last), "last_detected"
        lower = T - sch.stops[k].plan_ts
        return float(max(lower, last)), "lower_bound"

    def target(self, T: float) -> int:
        """Целевая остановка прогноза: первая с планом в (T+10, T+15] мин.

        Returns:
            int: Индекс или ``-1`` (нет расписания или остановки в окне — прогноз не строится).
        """
        if self.schedule is None:
            return -1
        return self.schedule.first_in_window(T + self.cfg.horizon_min_s, T + self.cfg.horizon_max_s)

    def dwell_s(self, T: float) -> float:
        """Длительность текущей стоянки, с (до последнего пакета, не до T: без связи простой не растёт)."""
        if self.standing_since is None:
            return 0.0
        return max(0.0, min(T, self.last_ts) - self.standing_since)

    def segment_info(self, T: float) -> dict:
        """Текущий сегмент между остановками.

        Returns:
            dict: ``from``, ``to``, ``avg_speed_kmh`` (факт), ``plan_speed_kmh``, ``dist_to_next_m``,
            ``progress`` (0..1); значения ``None``, если данных недостаточно.
        """
        sch = self.schedule
        info: dict = {"from": None, "to": None, "avg_speed_kmh": None, "plan_speed_kmh": None,
                      "dist_to_next_m": None, "progress": None}
        if sch is None or not self.initialized:
            return info
        n = len(sch.stops)
        prev_i = self.next_idx - 1
        nxt_i = self.next_idx if self.next_idx < n else None
        if prev_i >= 0:
            p = sch.stops[prev_i]
            info["from"] = {"idx": prev_i, "stop_key": p.stop_key, "address": p.address, "plan_ts": p.plan_ts}
        if nxt_i is not None:
            q = sch.stops[nxt_i]
            info["to"] = {"idx": nxt_i, "stop_key": q.stop_key, "address": q.address, "plan_ts": q.plan_ts}
            if self.lon is not None:
                info["dist_to_next_m"] = haversine_m(self.lon, self.lat, q.lon, q.lat)
        # < 20 с от начала сегмента скорость по 1–2 точкам слишком шумная
        if self.seg_start_ts is not None and self.last_ts > self.seg_start_ts + 20:
            info["avg_speed_kmh"] = self.seg_dist_m / (self.last_ts - self.seg_start_ts) * 3.6
        if prev_i >= 0 and nxt_i is not None:
            p, q = sch.stops[prev_i], sch.stops[nxt_i]
            dist = haversine_m(p.lon, p.lat, q.lon, q.lat)
            dt = q.plan_ts - p.plan_ts
            if dt > 0 and dist > 0:
                info["plan_speed_kmh"] = dist / dt * 3.6
                if info["dist_to_next_m"] is not None:
                    info["progress"] = max(0.0, min(1.0, 1 - info["dist_to_next_m"] / dist))
        return info

    def deviation_trend(self, n: int = 4) -> float | None:
        """Наклон отклонения по последним ``n`` детектированным остановкам.

        Returns:
            float | None: с/остановку; ``None`` при < 3 точках (тренд по двум — шум).
        """
        pts = sorted((a for a in self.arrivals.values() if a.detected), key=lambda a: a.idx)[-n:]
        if len(pts) < 3:
            return None
        return (pts[-1].delay_s - pts[0].delay_s) / (len(pts) - 1)

    def at_stop(self) -> bool:
        """ТС в геозоне предыдущей или следующей остановки (радиус ×1.5: остановочный карман шире точки)."""
        sch = self.schedule
        if sch is None or self.lon is None:
            return False
        for j in (self.next_idx - 1, self.next_idx):
            if 0 <= j < len(sch.stops):
                s = sch.stops[j]
                if haversine_m(self.lon, self.lat, s.lon, s.lat) <= self.cfg.stop_radius_m * 1.5:
                    return True
        return False

    def ml_history(self, T: float, rows: int) -> list[dict]:
        """Последние ``rows`` пакетов с ``event_time ≤ T`` в порядке поступления.

        Порядок поступления, а не сортировка: так же упорядочены строки traffic.csv при обучении
        (паритет признака ``speed_diff``).
        """
        cutoff = fmt_utc(T)  # строки одного формата сравниваются лексикографически = хронологически
        return [h for h in self.history if h["event_time"] <= cutoff][-rows:]
