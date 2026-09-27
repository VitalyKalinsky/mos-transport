"""Оценка риска и диагностика причины отклонения.

* Уровень риска (зелёный/жёлтый/красный) строится по прогнозу ML и вероятности опоздания.
  Пороги совпадают с ``target_class`` разметки: late > +120 с, early < −60 с.
* P(delay > 120 с): модель — регрессор с loss=MAE, поэтому её ошибка аппроксимируется распределением
  Лапласа с масштабом b = валидационная MAE (её публикует ML-сервис). Это калибровка поверх
  точечного прогноза, а не отдельная модель.
* Причина сбоя — правила по производным признакам телеметрии (простой, скорость на сегменте,
  связь/GPS, тренд). Рядом с ними дашборд показывает SHAP-паттерны модели из ML-сервиса (``docs/ML.md``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Settings


def laplace_sf(x: float, mu: float, b: float) -> float:
    """Функция выживания распределения Лапласа.

    Args:
        x: Порог (например, 120 с).
        mu: Центр — точечный прогноз модели, с.
        b: Масштаб — MAE модели, с. Для Лапласа MAE = b, поэтому калибровка точная.

    Returns:
        float: P(X > x), X ~ Laplace(mu, b).
    """
    b = max(b, 1e-6)  # защита от деления на ноль при нулевой MAE
    if x < mu:
        return 1.0 - 0.5 * math.exp((x - mu) / b)
    return 0.5 * math.exp(-(x - mu) / b)


def risk_level(pred: float, p_late: float, cfg: Settings) -> str:
    """Уровень риска для цветовой индикации.

    Args:
        pred: Прогноз задержки на целевой остановке, с.
        p_late: P(задержка > порога late).
        cfg: Настройки с порогами.

    Returns:
        str: ``"red"``, ``"yellow"`` или ``"green"``.
    """
    # красный: большое опоздание или уверенное умеренное
    if pred >= cfg.red_threshold_s or (pred >= cfg.late_threshold_s and p_late >= cfg.red_probability):
        return "red"
    # жёлтый включает опережение: раннее прибытие тоже нарушает интервал
    if pred >= cfg.late_threshold_s or pred <= cfg.early_threshold_s or p_late >= 0.5:
        return "yellow"
    return "green"


@dataclass
class Cause:
    """Причина отклонения для карточки инцидента.

    Attributes:
        code: Машинный код (``STANDING_OFF_STOP``, ``SLOW_SEGMENT``…).
        title: Заголовок для диспетчера.
        detail: Факты из телеметрии, подтверждающие причину.
        recommendation: Рекомендуемое действие диспетчера.
        weight: Значимость; определяет порядок (первая причина — основная).
    """

    code: str
    title: str
    detail: str
    recommendation: str
    weight: float


def diagnose(*, pred: float, cur_dev: float, dwell_s: float, at_stop: bool, seg: dict,
             speed: float, telemetry_age_s: float, gps_fail_streak: int, trend: float | None,
             is_peak: bool, source: str, cfg: Settings) -> list[Cause]:
    """Определяет вероятные причины отклонения по признакам телеметрии.

    Args:
        pred: Прогноз задержки, с.
        cur_dev: Текущее отклонение (на последней пройденной остановке), с.
        dwell_s: Длительность текущей стоянки, с.
        at_stop: ТС в геозоне остановки.
        seg: Текущий сегмент: ``avg_speed_kmh``, ``plan_speed_kmh``, …
        speed: Текущая скорость, км/ч.
        telemetry_age_s: Возраст последнего пакета, с (время данных).
        gps_fail_streak: Пакетов подряд без достоверных координат.
        trend: Средний прирост отклонения на остановку, с; ``None`` — мало данных.
        is_peak: Сейчас час пик.
        source: ``"model"`` или ``"fallback"``.
        cfg: Настройки с порогами.

    Returns:
        list[Cause]: Причины по убыванию ``weight``; список никогда не пуст.
    """
    causes: list[Cause] = []
    delta = pred - cur_dev  # сколько модель добавляет к текущему отклонению за 10–15 мин

    if telemetry_age_s > 120:
        causes.append(Cause(
            "NO_SIGNAL", "Нет связи с бортовым терминалом",
            f"Последний пакет {int(telemetry_age_s // 60)} мин назад; прогноз по последнему известному состоянию",
            "Проверить связь/терминал, связаться с водителем по радио", 90))
    if gps_fail_streak >= 4:  # 1–3 пакета без фикса — норма в тоннелях и у высоток
        causes.append(Cause(
            "GPS_FAILURE", "Сбой навигации (GPS)",
            f"{gps_fail_streak} пакетов подряд без достоверных координат",
            "Проверить навигационный блок; положение ТС на карте оценочное", 60))
    # 90 с — дольше обычного светофорного цикла, значит стоит не на светофоре
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
    # plan > 5: на коротких перегонах плановая скорость шумная; dwell < 90 — простой уже учтён выше
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

    if not causes:  # карточка инцидента всегда должна содержать хотя бы одну причину
        causes.append(Cause(
            "MODEL", "Отклонение по прогнозу модели",
            "Явных аномалий в телеметрии не выявлено", "Взять ТС на контроль", 10))
    causes.sort(key=lambda c: -c.weight)
    return causes
