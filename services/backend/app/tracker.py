"""Состояние ТС: наложение телеметрии на нитку графика и производные признаки.

* Детекция фактического прибытия на остановку — геозона `stop_radius_m` вокруг точки остановки,
  последовательное сопоставление по порядку расписания (окно lookahead), чтобы кольцевые
  маршруты и остановки встречного направления не давали ложных срабатываний.
* Текущее отклонение (cur_dev_s) — по определению разметки: задержка на последней остановке,
  плановое время которой ≤ T. Если ТС до неё ещё не доехало — нижняя оценка (T − план).
* Производные признаки для диспетчера: средняя скорость на сегменте, плановая скорость сегмента,
  время простоя, возраст телеметрии, серия сбоев GPS, тренд отклонения.
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
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


@dataclass(slots=True)
class Arrival:
    idx: int
    fact_ts: float
    delay_s: float
    detected: bool = True     # False — остановка пропущена детектором (нет GPS в геозоне)


@dataclass
class VehicleState:
    unit_id: int
    tr_id: int | None
    schedule: VehicleSchedule | None
    cfg: Settings

    # последняя телеметрия
    last_ts: float = 0.0                 # event_time последнего пакета (data-time)
    last_rx_wall: float = 0.0            # когда пакет получен (wall-clock, для контроля связи)
    lon: float | None = None
    lat: float | None = None
    pos_ts: float = 0.0                  # время последней валидной координаты
    speed: float = 0.0
    heading: float = 0.0
    location_valid: bool = False
    gps_fail_streak: int = 0
    packets: int = 0

    history: deque = field(default_factory=lambda: deque(maxlen=40))   # сырые строки для ML

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

    # прогноз
    prediction: dict | None = None
    pred_trail: deque = field(default_factory=lambda: deque(maxlen=180))
    pending_eval: dict[int, list[tuple[float, float, float]]] = field(default_factory=dict)
    dirty: bool = True

    # ------------------------------------------------------------------ ingest
    def reset_schedule_state(self) -> None:
        self.next_idx = 0
        self.initialized = False
        self.arrivals.clear()
        self.last_arrival = None
        self.seg_start_ts = None
        self.seg_dist_m = 0.0
        self.standing_since = None
        self.pending_eval.clear()
        self.pred_trail.clear()
        self.history.clear()
        self.prediction = None

    def ingest(self, ts: float, lon: float | None, lat: float | None, valid: bool,
               speed: float, heading: float, alt: float, is_hist: bool,
               rx_wall: float | None = None) -> list[Arrival]:
        """Применяет пакет телеметрии. Возвращает список новых прибытий на остановки."""
        rx_wall = rx_wall or time.time()
        # скачок времени назад > 1 ч (перезапуск реплея/эмулятора) → начинаем нитку заново
        if self.last_ts and ts < self.last_ts - 3600:
            self.reset_schedule_state()
            self.last_ts = 0.0
        self.packets += 1
        self.last_rx_wall = rx_wall
        self.dirty = True

        has_pos = valid and lon is not None and lat is not None
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

        if ts < self.last_ts:            # опоздавший (исторический) пакет — только в историю
            return []
        prev_lon, prev_lat, prev_pos_ts = self.lon, self.lat, self.pos_ts
        self.last_ts = ts

        if not has_pos:
            self.gps_fail_streak += 1
            self.location_valid = False
            return []

        self.gps_fail_streak = 0
        self.location_valid = True
        self.lon, self.lat, self.pos_ts = lon, lat, ts
        self.speed, self.heading = speed, heading

        # простой
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
        sch = self.schedule
        if sch is None or not sch.stops:
            return []
        stops = sch.stops
        n = len(stops)
        if not self.initialized:
            # первая привязка: часть нитки, которую ТС ещё может проходить
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
            if s.plan_ts - ts > self.cfg.max_early_s:
                break
            hit_ts = self._passage_time(s.lon, s.lat, lon, lat, ts, prev)
            if hit_ts is not None:
                hit = (j, hit_ts)
                break
        if hit is None:
            return []

        j, hit_ts = hit
        for k in range(self.next_idx, j):
            self._skip(k)
        arr = Arrival(j, hit_ts, hit_ts - stops[j].plan_ts, True)
        self.arrivals[j] = arr
        self.last_arrival = arr
        self.arrivals_detected += 1
        self.next_idx = j + 1
        self.seg_start_ts = ts
        self.seg_dist_m = 0.0
        return [arr]

    def _passage_time(self, slon: float, slat: float, lon: float, lat: float, ts: float,
                      prev: tuple[float, float, float] | None) -> float | None:
        """Время прохождения геозоны остановки.

        Точки GPS приходят раз в 10–15 с (60–80 м пути), поэтому проверяем не только точку,
        но и отрезок трека prev→cur: если он проходит в пределах радиуса — берём момент
        ближайшего подхода (линейная интерполяция по времени).
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
            # оценка: переносим последнее известное отклонение (для cur_dev_s, помечено detected=False)
            last = self.last_arrival.delay_s if self.last_arrival else 0.0
            self.arrivals[k] = Arrival(k, self.schedule.stops[k].plan_ts + last, last, False)

    # --------------------------------------------------------- derived features
    def current_deviation(self, T: float) -> tuple[float, str]:
        """cur_dev_s на момент T и способ его получения."""
        sch = self.schedule
        if sch is None:
            return 0.0, "no_schedule"
        k = sch.last_planned_before(T)
        if k < 0:
            return 0.0, "before_start"
        arr = self.arrivals.get(k)
        if arr is not None and arr.fact_ts <= T:
            return float(arr.delay_s), ("fact" if arr.detected else "carried")
        # ТС ещё не прибыло на остановку с плановым временем ≤ T → опоздание не меньше (T − план).
        # (ETA по остаточному пути проверялась офлайн — точность хуже, см. scripts/validate_matching.py)
        if not self.initialized:
            return 0.0, "before_start"
        last = self.last_arrival.delay_s if self.last_arrival else 0.0
        if self.cfg.cur_dev_mode == "last_detected":
            return float(last), "last_detected"
        lower = T - sch.stops[k].plan_ts
        return float(max(lower, last)), "lower_bound"

    def target(self, T: float) -> int:
        if self.schedule is None:
            return -1
        return self.schedule.first_in_window(T + self.cfg.horizon_min_s, T + self.cfg.horizon_max_s)

    def dwell_s(self, T: float) -> float:
        if self.standing_since is None:
            return 0.0
        return max(0.0, min(T, self.last_ts) - self.standing_since)

    def segment_info(self, T: float) -> dict:
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
        """Наклон отклонения по последним n детектированным остановкам, с/остановку."""
        pts = sorted((a for a in self.arrivals.values() if a.detected), key=lambda a: a.idx)[-n:]
        if len(pts) < 3:
            return None
        return (pts[-1].delay_s - pts[0].delay_s) / (len(pts) - 1)

    def at_stop(self) -> bool:
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
        """Последние пакеты с event_time ≤ T в порядке поступления (как в потоке)."""
        cutoff = fmt_utc(T)
        return [h for h in self.history if h["event_time"] <= cutoff][-rows:]
