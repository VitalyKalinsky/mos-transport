"""Конфигурация Backend-сервиса: каждый параметр переопределяется переменной окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default, cast=str):
    """Читает переменную окружения с приведением типа.

    Args:
        name: Имя переменной.
        default: Значение, если переменная не задана или пуста.
        cast: Тип (``str``, ``int``, ``float``, ``bool``).

    Returns:
        Значение переменной, приведённое к ``cast``, либо ``default``.

    Raises:
        ValueError: Если значение не приводится к ``cast`` (ошибка конфигурации видна на старте).
    """
    v = os.getenv(name)
    if v is None or v == "":  # пустая строка из compose = «не задано»
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "on")
    return cast(v)


@dataclass(frozen=True)
class Settings:
    """Параметры сервиса. ``frozen`` — конфиг не меняется в рантайме."""

    # --- справочные данные: эталонное расписание и привязка терминал (unit_id) → ТС (tr_id)
    schedule_path: str = field(default_factory=lambda: _env("SCHEDULE_PATH", "/data/validate/schedule_plan.csv"))
    units_path: str = field(default_factory=lambda: _env("UNITS_PATH", "/data/validate/traffic.csv"))

    # --- приём NDTP
    ndtp_host: str = field(default_factory=lambda: _env("NDTP_HOST", "0.0.0.0"))
    ndtp_port: int = field(default_factory=lambda: _env("NDTP_PORT", 9201, int))
    ndtp_verify_crc: bool = field(default_factory=lambda: _env("NDTP_VERIFY_CRC", True, bool))
    # терминал шлёт пакет раз в 5–15 с; 120 с тишины = мёртвое TCP-соединение, закрываем
    ndtp_idle_timeout_s: float = field(default_factory=lambda: _env("NDTP_IDLE_TIMEOUT_S", 120.0, float))

    # --- ML-сервис
    ml_url: str = field(default_factory=lambda: _env("ML_URL", "http://ml-service:8001"))
    # таймаут < периода цикла прогноза: зависший ML не должен тормозить поток
    ml_timeout_s: float = field(default_factory=lambda: _env("ML_TIMEOUT_S", 1.5, float))
    ml_breaker_failures: int = field(default_factory=lambda: _env("ML_BREAKER_FAILURES", 3, int))
    ml_breaker_cooldown_s: float = field(default_factory=lambda: _env("ML_BREAKER_COOLDOWN_S", 5.0, float))
    # признаки ML-ядра смотрят на 5 мин назад; 20 пакетов (~3–5 мин) хватает и держат запрос маленьким
    ml_history_rows: int = field(default_factory=lambda: _env("ML_HISTORY_ROWS", 20, int))

    # --- цикл прогноза (событийный: не чаще min, не реже max)
    predict_interval_s: float = field(default_factory=lambda: _env("PREDICT_INTERVAL_S", 1.0, float))
    predict_min_interval_s: float = field(default_factory=lambda: _env("PREDICT_MIN_INTERVAL_S", 0.15, float))
    horizon_min_s: int = field(default_factory=lambda: _env("HORIZON_MIN_S", 600, int))    # T+10 мин
    horizon_max_s: int = field(default_factory=lambda: _env("HORIZON_MAX_S", 900, int))    # T+15 мин

    # --- сопоставление с расписанием
    stop_radius_m: float = field(default_factory=lambda: _env("STOP_RADIUS_M", 45.0, float))
    stop_lookahead: int = field(default_factory=lambda: _env("STOP_LOOKAHEAD", 25, int))
    # окно [−8, +12] мин от плана: отсекает повторный проход той же остановки на кольце
    max_early_s: int = field(default_factory=lambda: _env("MAX_EARLY_S", 480, int))
    max_late_s: int = field(default_factory=lambda: _env("MAX_LATE_S", 720, int))
    # оценка cur_dev_s, пока ТС не доехало до остановки с планом ≤ T:
    # lower_bound — max(T − план, последнее отклонение); last_detected — последнее детектированное
    # (выбран по MAE онлайн-контура, см. scripts/evaluate_online.py)
    cur_dev_mode: str = field(default_factory=lambda: _env("CUR_DEV_MODE", "last_detected"))
    standing_speed_kmh: float = field(default_factory=lambda: _env("STANDING_SPEED_KMH", 3.0, float))

    # --- риск (пороги совпадают с target_class разметки)
    late_threshold_s: float = field(default_factory=lambda: _env("LATE_THRESHOLD_S", 120.0, float))
    early_threshold_s: float = field(default_factory=lambda: _env("EARLY_THRESHOLD_S", -60.0, float))
    red_threshold_s: float = field(default_factory=lambda: _env("RED_THRESHOLD_S", 240.0, float))
    red_probability: float = field(default_factory=lambda: _env("RED_PROBABILITY", 0.75, float))
    # масштаб ошибки до первого ответа ML (затем берётся валидационная MAE модели)
    default_error_scale_s: float = field(default_factory=lambda: _env("DEFAULT_ERROR_SCALE_S", 70.0, float))

    # --- надёжность / деградация
    link_timeout_s: float = field(default_factory=lambda: _env("LINK_TIMEOUT_S", 15.0, float))
    vehicle_stale_s: float = field(default_factory=lambda: _env("VEHICLE_STALE_S", 120.0, float))  # время данных
    unknown_unit_ttl_s: float = field(default_factory=lambda: _env("UNKNOWN_UNIT_TTL_S", 300.0, float))
    clock_freerun_max_s: float = field(default_factory=lambda: _env("CLOCK_FREERUN_MAX_S", 3 * 3600.0, float))

    # --- ETA / таймлайн
    eta_stops: int = field(default_factory=lambda: _env("ETA_STOPS", 8, int))
    timeline_step_s: float = field(default_factory=lambda: _env("TIMELINE_STEP_S", 15.0, float))
    timeline_frames: int = field(default_factory=lambda: _env("TIMELINE_FRAMES", 1440, int))   # 6 ч данных

    # --- справочники
    region: str = field(default_factory=lambda: _env("REGION", "moscow"))
    regions_path: str = field(default_factory=lambda: _env("REGIONS_PATH", "/app/data/regions/regions.json"))
    routes_dir: str = field(default_factory=lambda: _env("ROUTES_DIR", "/var/lib/mos-transport/routes"))  # volume
    builtin_routes_dir: str = field(default_factory=lambda: _env("BUILTIN_ROUTES_DIR", "/app/data/routes"))
    feeder_url: str = field(default_factory=lambda: _env("FEEDER_URL", "http://feeder:8090"))

    # --- отображение
    display_tz_offset_h: int = field(default_factory=lambda: _env("DISPLAY_TZ_OFFSET_H", 3, int))  # МСК


settings = Settings()
