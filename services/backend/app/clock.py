"""Часы «времени данных».

Телеметрия несёт свои метки времени: эмулятор — текущее время, реплей датасета — 2026-01-06
(возможно, с ускорением). Момент прогноза T и сопоставление с расписанием считаются во времени
данных, а не по системным часам. Часы:

* синхронизируются по максимальной метке пакетов известных ТС;
* оценивают темп (сек данных / сек wall-clock): реплей ×10 даёт темп ≈ 10;
* при обрыве связи идут дальше с последним темпом (free-run), чтобы прогноз деградировал честно:
  растёт возраст телеметрии, и модель переходит на исторические признаки.
"""
from __future__ import annotations

import time


class DataClock:
    """Часы времени данных с экстраполяцией между пакетами.

    Args:
        freerun_max_s: Предел хода без пакетов (сек данных), чтобы при долгом обрыве
            время не «улетало» за пределы расписания.

    Attributes:
        rate (float): Оценка темпа потока (сек данных за секунду wall-clock).
    """

    def __init__(self, freerun_max_s: float = 3 * 3600.0) -> None:
        self.anchor_data: float | None = None
        self.anchor_wall: float = 0.0
        self.rate: float = 1.0
        self.freerun_max_s = freerun_max_s
        self._win: list[tuple[float, float]] = []   # (wall, data) за ~20 с для оценки темпа

    @property
    def synced(self) -> bool:
        """bool: Получен ли хотя бы один пакет с меткой времени."""
        return self.anchor_data is not None

    def observe(self, data_ts: float, wall: float | None = None) -> None:
        """Учитывает метку времени пришедшего пакета.

        Args:
            data_ts: Время пакета (unix, с, время данных).
            wall: Время приёма (unix, с); по умолчанию — текущее.
        """
        wall = wall or time.time()
        if self.anchor_data is not None and data_ts < self.anchor_data - 3600:
            # скачок назад > 1 ч = реплей начался заново → пересинхронизация
            self.anchor_data, self._win = None, []
        # якорь — только максимальная метка: пакеты из «хвоста» (история, опоздавшие) время не откатывают
        if self.anchor_data is None or data_ts >= self.anchor_data:
            self.anchor_data, self.anchor_wall = data_ts, wall
            self._win.append((wall, data_ts))
            cutoff = wall - 20.0
            while len(self._win) > 2 and self._win[0][0] < cutoff:
                self._win.pop(0)
            if len(self._win) >= 2:
                (w0, d0), (w1, d1) = self._win[0], self._win[-1]
                if w1 - w0 >= 3.0:  # база < 3 с даёт шумный темп из-за джиттера пакетов
                    self.rate = max(0.1, min(1000.0, (d1 - d0) / (w1 - w0)))

    def now(self, wall: float | None = None) -> float:
        """Текущее время данных.

        Args:
            wall: Момент wall-clock (unix, с); по умолчанию — текущий.

        Returns:
            float: Время данных (unix, с). До первого пакета — системное время.
        """
        wall = wall or time.time()
        if self.anchor_data is None:
            return wall
        elapsed = min(self.freerun_max_s, (wall - self.anchor_wall) * self.rate)
        return self.anchor_data + max(0.0, elapsed)

    def freerun_s(self, wall: float | None = None) -> float:
        """Сколько секунд данных часы идут без подтверждения пакетами.

        Args:
            wall: Момент wall-clock (unix, с); по умолчанию — текущий.

        Returns:
            float: Секунды времени данных с последнего якоря; 0 до синхронизации.
        """
        wall = wall or time.time()
        if self.anchor_data is None:
            return 0.0
        return (wall - self.anchor_wall) * self.rate
