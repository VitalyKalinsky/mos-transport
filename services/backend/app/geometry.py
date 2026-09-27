"""Геометрия полилиний: длина, проекция точки на линию, точка на заданном расстоянии.

Координаты — ``[[lat, lon], ...]``, как в Leaflet. Для расчётов в масштабе города используется
локальная равнопромежуточная проекция: ошибка < 0.1% на отрезках до десятков км, при этом
никаких зависимостей вроде pyproj/shapely.
"""
from __future__ import annotations

import math
from bisect import bisect_right

from .geo import haversine_m


class Polyline:
    """Полилиния с накопленными длинами для O(log n) поиска точки по расстоянию.

    Args:
        coords: Вершины ``[[lat, lon], ...]``.

    Attributes:
        coords (list[list[float]]): Вершины без подряд идущих дубликатов.
        cum (list[float]): Накопленная длина до каждой вершины, м.
        length (float): Полная длина, м.
    """

    __slots__ = ("coords", "cum", "length")  # тысячи перегонов в памяти — экономим на __dict__

    def __init__(self, coords: list[list[float]]) -> None:
        # дубликаты дают сегменты нулевой длины и деление на ноль в проекции
        pts: list[list[float]] = []
        for c in coords:
            if not pts or (abs(pts[-1][0] - c[0]) > 1e-9 or abs(pts[-1][1] - c[1]) > 1e-9):
                pts.append([float(c[0]), float(c[1])])
        if len(pts) == 1:  # вырожденная линия из одной точки → отрезок нулевой длины
            pts.append(list(pts[0]))
        self.coords = pts
        self.cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            self.cum.append(self.cum[-1] + haversine_m(a[1], a[0], b[1], b[0]))
        self.length = self.cum[-1]

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """Проекция точки на линию.

        Args:
            lat: Широта точки.
            lon: Долгота точки.

        Returns:
            tuple[float, float]: (расстояние вдоль линии до ближайшей точки, расстояние от точки до линии), м.
        """
        # метры на градус в окрестности точки: локальная проекция вместо haversine на каждом сегменте
        kx = 111320.0 * math.cos(math.radians(lat))
        ky = 110540.0
        best = (0.0, float("inf"))
        for i, (a, b) in enumerate(zip(self.coords, self.coords[1:])):
            ax, ay = (a[1] - lon) * kx, (a[0] - lat) * ky
            bx, by = (b[1] - lon) * kx, (b[0] - lat) * ky
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            u = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            cx, cy = ax + u * dx, ay + u * dy
            d = math.hypot(cx, cy)
            if d < best[1]:
                best = (self.cum[i] + u * (self.cum[i + 1] - self.cum[i]), d)
        return best

    def point_at(self, s: float) -> list[float]:
        """Точка на расстоянии ``s`` от начала линии.

        Args:
            s: Расстояние вдоль линии, м; значение обрезается до ``[0, length]``.

        Returns:
            list[float]: ``[lat, lon]``.
        """
        if s <= 0:
            return list(self.coords[0])
        if s >= self.length:
            return list(self.coords[-1])
        i = bisect_right(self.cum, s) - 1
        seg = self.cum[i + 1] - self.cum[i]
        u = 0.0 if seg == 0 else (s - self.cum[i]) / seg
        a, b = self.coords[i], self.coords[i + 1]
        return [a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])]

    def slice(self, s0: float, s1: float) -> list[list[float]]:
        """Участок линии между двумя расстояниями.

        Args:
            s0: Начало участка, м.
            s1: Конец участка, м.

        Returns:
            list[list[float]]: Вершины участка; при ``s1 <= s0`` — одна точка.
        """
        s0, s1 = max(0.0, s0), min(self.length, s1)
        if s1 <= s0:
            return [self.point_at(s0)]
        i0 = bisect_right(self.cum, s0)
        i1 = bisect_right(self.cum, s1)
        return [self.point_at(s0)] + [list(c) for c in self.coords[i0:i1]] + [self.point_at(s1)]


def simplify(coords: list[list[float]], tol_m: float = 3.0) -> list[list[float]]:
    """Упрощение Дугласа–Пекера для компактного хранения и передачи геометрии.

    Args:
        coords: Вершины ``[[lat, lon], ...]``.
        tol_m: Допуск отклонения, м; 3 м ниже точности GPS и незаметны на карте.

    Returns:
        list[list[float]]: Подмножество исходных вершин (первая и последняя сохраняются).
    """
    if len(coords) < 3:
        return coords
    lat0 = coords[0][0]
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    pts = [((c[1]) * kx, (c[0]) * ky) for c in coords]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]  # итеративно: рекурсия на треках в тысячи точек упирается в лимит стека Python
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1e-9
        idx, dmax = -1, 0.0
        for k in range(i + 1, j):
            px, py = pts[k]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / n
            if d > dmax:
                idx, dmax = k, d
        if dmax > tol_m and idx > 0:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [c for c, k in zip(coords, keep) if k]


def remove_spurs(coords: list[list[float]], close_m: float = 15.0, max_pts: int = 12) -> list[list[float]]:
    """Убирает «усы»: заезды туда-обратно при привязке остановки к боковому проезду.

    Если линия возвращается ближе ``close_m`` к уже пройденной точке, сделав петлю не более чем
    из ``max_pts`` вершин, петля вырезается.

    Args:
        coords: Вершины ``[[lat, lon], ...]``.
        close_m: Порог «вернулась в ту же точку», м.
        max_pts: Максимальная длина петли в вершинах; длиннее — это реальный разворот, а не ус.

    Returns:
        list[list[float]]: Вершины без усов.
    """
    from .geo import haversine_m
    pts = [list(c) for c in coords]
    i = 0
    while i < len(pts) - 2:
        cut = None
        for j in range(min(len(pts) - 1, i + max_pts), i + 1, -1):
            if haversine_m(pts[i][1], pts[i][0], pts[j][1], pts[j][0]) < close_m:
                loop = sum(haversine_m(pts[k][1], pts[k][0], pts[k + 1][1], pts[k + 1][0]) for k in range(i, j))
                if loop > 3 * close_m:  # короткий «зигзаг» — шум GPS, его оставляет simplify
                    cut = j
                    break
        if cut is not None:
            del pts[i + 1:cut]            # конечную точку петли сохраняем: она может быть остановкой
        else:
            i += 1
    return pts
