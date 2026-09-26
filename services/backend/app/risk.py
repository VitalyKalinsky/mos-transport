"""Оценка риска и диагностика причины отклонения.

* Уровень риска (зелёный/жёлтый/красный) — по прогнозу задержки ML-модели и вероятности
  опоздания. Пороги совпадают с разметкой target_class: late > +120 с, early < −60 с.
* Вероятность опоздания P(delay > 120 с): модель — регрессор с функцией потерь MAE, поэтому
  ошибка аппроксимируется распределением Лапласа с масштабом b = MAE модели на валидации
  (публикуется ML-сервисом). Это калибровка поверх точечного прогноза, а не отдельная ML-модель.
* Причина/паттерн сбоя — rule-based диагностика по производным признакам телеметрии
  (простой, скорость на сегменте, связь/GPS, тренд отклонения). ML-классификатора причин
  в ML-ядре нет — см. README, раздел «Что требуется от ML-части».
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Settings


def laplace_sf(x: float, mu: float, b: float) -> float:
    """P(X > x), X ~ Laplace(mu, b)."""
    b = max(b, 1e-6)
    if x < mu:
        return 1.0 - 0.5 * math.exp((x - mu) / b)
    return 0.5 * math.exp(-(x - mu) / b)


def risk_level(pred: float, p_late: float, cfg: Settings) -> str:
    if pred >= cfg.red_threshold_s or (pred >= cfg.late_threshold_s and p_late >= cfg.red_probability):
        return "red"
    if pred >= cfg.late_threshold_s or pred <= cfg.early_threshold_s or p_late >= 0.5:
        return "yellow"
    return "green"


@dataclass
class Cause:
    code: str
    title: str
    detail: str
    recommendation: str
    weight: float


def diagnose(*, pred: float, cur_dev: float, dwell_s: float, at_stop: bool, seg: dict,
             speed: float, telemetry_age_s: float, gps_fail_streak: int, trend: float | None,
             is_peak: bool, source: str, cfg: Settings) -> list[Cause]:
    """Возвращает причины, отсортированные по значимости (первая — основная)."""
    causes: list[Cause] = []
    delta = pred - cur_dev

    if telemetry_age_s > 120:
        causes.append(Cause(
            "NO_SIGNAL", "Нет связи с бортовым терминалом",
            f"Последний пакет {int(telemetry_age_s // 60)} мин назад; прогноз по последнему известному состоянию",
            "Проверить связь/терминал, связаться с водителем по радио", 90))
    if gps_fail_streak >= 4:
        causes.append(Cause(
            "GPS_FAILURE", "Сбой навигации (GPS)",
            f"{gps_fail_streak} пакетов подряд без достоверных координат",
            "Проверить навигационный блок; положение ТС на карте оценочное", 60))
    if dwell_s >= 90 and not at_stop:
        causes.append(Cause(
            "STANDING_OFF_STOP", "Длительный простой вне остановки",
            f"ТС стоит {int(dwell_s // 60)} мин {int(dwell_s % 60)} с вне остановочного пункта — вероятен затор, ДТП или неисправность",
            "Связаться с водителем; при ДТП/поломке — направить резервное ТС, предупредить следующие рейсы", 85 + min(dwell_s / 30, 15)))
    elif dwell_s >= 90 and at_stop:
        causes.append(Cause(
            "LONG_DWELL", "Затянувшаяся стоянка на остановке",
            f"Стоянка {int(dwell_s // 60)} мин {int(dwell_s % 60)} с — высокий пассажиропоток или задержка отправления",
            "Дать водителю команду на отправление; при пассажиропотоке — усилить выпуск", 70 + min(dwell_s / 30, 15)))

    avg, plan = seg.get("avg_speed_kmh"), seg.get("plan_speed_kmh")
    if avg is not None and plan and plan > 5 and avg < 0.6 * plan and dwell_s < 90:
        causes.append(Cause(
            "SLOW_SEGMENT", "Низкая скорость на участке (затор)",
            f"Средняя скорость на сегменте {avg:.0f} км/ч при плановой {plan:.0f} км/ч",
            "Предупредить водителей следующих рейсов; рассмотреть объезд или корректировку интервалов", 65 + (1 - avg / plan) * 20))

    if trend is not None and trend > 20:
        causes.append(Cause(
            "ACCUMULATING", "Нарастание отставания от графика",
            f"Отклонение растёт в среднем на {trend:.0f} с на каждой остановке",
            "Регулирование: сократить стоянку на конечной, выровнять интервал резервным ТС", 55 + min(trend, 40)))

    if cur_dev >= cfg.late_threshold_s and pred >= cfg.late_threshold_s and delta < 60:
        recovering = delta <= -30
        causes.append(Cause(
            "CARRY_OVER", "Опоздание унаследовано с предыдущих участков",
            f"ТС уже опаздывает на {int(cur_dev)} с; " + (
                f"модель ожидает, что оно нагонит лишь {int(-delta)} с" if recovering
                else "модель не ожидает восстановления графика"),
            "Скорректировать отправление с конечной; информировать пассажиров через табло", 50))
    elif delta >= 60:
        causes.append(Cause(
            "PREDICTED_GROWTH", "Модель ожидает рост задержки на участке",
            f"Прогноз: +{int(delta)} с к текущему отклонению за 10–15 мин",
            "Взять ТС на контроль; при подтверждении — регулировать интервал", 45 + min(delta / 10, 20)))

    if pred <= cfg.early_threshold_s:
        causes.append(Cause(
            "EARLY", "Опережение графика",
            f"Прогноз: прибытие раньше плана на {int(-pred)} с",
            "Дать водителю команду выдержать стоянку / снизить темп движения", 60))

    if is_peak and pred >= cfg.late_threshold_s:
        causes.append(Cause(
            "PEAK", "Пиковая нагрузка на сеть",
            "Час пик: повышенный пассажиропоток и трафик",
            "Учитывать при регулировании; возможен выпуск дополнительных ТС", 30))

    if source != "model":
        causes.append(Cause(
            "ML_FALLBACK", "Прогноз в упрощённом режиме",
            "ML-сервис недоступен — прогноз = текущее отклонение (baseline)",
            "Проверить состояние ML-сервиса", 20))

    if not causes:
        causes.append(Cause(
            "MODEL", "Отклонение по прогнозу модели",
            "Явных аномалий в телеметрии не выявлено", "Взять ТС на контроль", 10))
    causes.sort(key=lambda c: -c.weight)
    return causes
