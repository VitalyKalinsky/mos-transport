"""Лёгкие метрики производительности в памяти (без Prometheus-клиента: ноль зависимостей)."""
from __future__ import annotations

import time
from collections import deque


class LatencyWindow:
    """Скользящее окно замеров задержки для перцентилей p50/p95/p99.

    Args:
        maxlen: Сколько последних замеров хранить; ограничивает память при бесконечном потоке.
    """

    def __init__(self, maxlen: int = 2000) -> None:
        self._v: deque[float] = deque(maxlen=maxlen)

    def add(self, ms: float) -> None:
        """Добавляет замер.

        Args:
            ms: Длительность в миллисекундах.
        """
        self._v.append(ms)

    def summary(self) -> dict:
        """Сводка по окну.

        Returns:
            dict: ``n``, ``p50``, ``p95``, ``p99``, ``max`` (мс); при пустом окне значения ``None``.
        """
        v = sorted(self._v)  # сортировка 2000 элементов дешевле поддержки t-digest
        if not v:
            return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}

        def q(p: float) -> float:
            return round(v[min(len(v) - 1, int(p * len(v)))], 3)

        return {"n": len(v), "p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "max": round(v[-1], 3)}


class RateCounter:
    """Счётчик событий в секунду за последние ``window`` секунд.

    Args:
        window: Ширина окна усреднения, с.

    Attributes:
        total (int): Общее число событий с запуска.
    """

    def __init__(self, window: int = 30) -> None:
        self.window = window
        self._buckets: deque[list] = deque()   # [секунда, count] — O(window) памяти
        self.total = 0

    def hit(self, n: int = 1, now: float | None = None) -> None:
        """Регистрирует ``n`` событий.

        Args:
            n: Число событий.
            now: Время события (unix, с); по умолчанию — текущее.
        """
        sec = int(now or time.time())
        self.total += n
        if self._buckets and self._buckets[-1][0] == sec:
            self._buckets[-1][1] += n
        else:
            self._buckets.append([sec, n])
        self._trim(sec)

    def _trim(self, sec: int) -> None:
        while self._buckets and self._buckets[0][0] <= sec - self.window:
            self._buckets.popleft()

    def rate(self, now: float | None = None) -> float:
        """Средняя частота за окно.

        Args:
            now: Момент оценки (unix, с); по умолчанию — текущий.

        Returns:
            float: События в секунду.
        """
        sec = int(now or time.time())
        self._trim(sec)
        return sum(c for _, c in self._buckets) / self.window
