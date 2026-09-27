"""Геодезические расчёты на сфере (без внешних зависимостей: вызывается на каждый пакет)."""
from __future__ import annotations

import math

_R = 6371000.0  # средний радиус Земли, м; погрешность сферы < 0.5% — для геозон 45 м достаточно


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Расстояние по большому кругу между двумя точками.

    Args:
        lon1: Долгота первой точки, градусы.
        lat1: Широта первой точки, градусы.
        lon2: Долгота второй точки, градусы.
        lat2: Широта второй точки, градусы.

    Returns:
        float: Расстояние в метрах.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    # min(1.0, …) — защита asin от a > 1 из-за погрешности float на антиподах
    return 2 * _R * math.asin(min(1.0, math.sqrt(a)))
