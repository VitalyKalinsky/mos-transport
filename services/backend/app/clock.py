"""Часы «времени данных».

Телеметрия несёт собственные метки времени (эмулятор — текущее время, реплей датасета —
время 2026-01-06, возможно с ускорением). Момент прогноза T и сопоставление с расписанием
идут во времени данных. Часы:
  * синхронизируются по максимальной метке пакетов известных ТС;
  * оценивают темп (сек данных / сек wall-clock) — реплей ×10 даёт темп ≈ 10;
  * при обрыве связи продолжают идти с последним темпом (free-run), чтобы прогнозы
    деградировали честно: растёт возраст телеметрии, модель переходит на исторические признаки.
"""
from __future__ import annotations

import time


class DataClock:
    def __init__(self, freerun_max_s: float = 3 * 3600.0) -> None:
        self.anchor_data: float | None = None
        self.anchor_wall: float = 0.0
        self.rate: float = 1.0
        self.freerun_max_s = freerun_max_s
        self._win: list[tuple[float, float]] = []   # (wall, data) для оценки темпа

    @property
    def synced(self) -> bool:
        return self.anchor_data is not None

    def observe(self, data_ts: float, wall: float | None = None) -> None:
        wall = wall or time.time()
        if self.anchor_data is not None and data_ts < self.anchor_data - 3600:
            # перезапуск источника (реплей пошёл заново) → пересинхронизация
            self.anchor_data, self._win = None, []
        # якорь — максимальная метка времени пакетов; между пакетами часы экстраполируются с темпом rate
        if self.anchor_data is None or data_ts >= self.anchor_data:
            self.anchor_data, self.anchor_wall = data_ts, wall
            self._win.append((wall, data_ts))
            cutoff = wall - 20.0
            while len(self._win) > 2 and self._win[0][0] < cutoff:
                self._win.pop(0)
            if len(self._win) >= 2:
                (w0, d0), (w1, d1) = self._win[0], self._win[-1]
                if w1 - w0 >= 3.0:
                    self.rate = max(0.1, min(1000.0, (d1 - d0) / (w1 - w0)))

    def now(self, wall: float | None = None) -> float:
        wall = wall or time.time()
        if self.anchor_data is None:
            return wall
        elapsed = min(self.freerun_max_s, (wall - self.anchor_wall) * self.rate)
        return self.anchor_data + max(0.0, elapsed)

    def freerun_s(self, wall: float | None = None) -> float:
        """Сколько секунд данных часы идут без подтверждения пакетами."""
        wall = wall or time.time()
        if self.anchor_data is None:
            return 0.0
        return (wall - self.anchor_wall) * self.rate
