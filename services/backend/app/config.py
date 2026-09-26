"""Конфигурация Backend-сервиса (все параметры переопределяются переменными окружения)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default, cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.lower() in ("1", "true", "yes", "on")
    return cast(v)


@dataclass(frozen=True)
class Settings:
    # --- справочные данные (эталонное расписание и привязка терминал → ТС)
    schedule_path: str = field(default_factory=lambda: _env("SCHEDULE_PATH", "/data/validate/schedule_plan.csv"))
    units_path: str = field(default_factory=lambda: _env("UNITS_PATH", "/data/validate/traffic.csv"))

    # --- приём NDTP
    ndtp_host: str = field(default_factory=lambda: _env("NDTP_HOST", "0.0.0.0"))
    ndtp_port: int = field(default_factory=lambda: _env("NDTP_PORT", 9201, int))
    ndtp_verify_crc: bool = field(default_factory=lambda: _env("NDTP_VERIFY_CRC", True, bool))
    ndtp_idle_timeout_s: float = field(default_factory=lambda: _env("NDTP_IDLE_TIMEOUT_S", 120.0, float))

    # --- ML-сервис
    ml_url: str = field(default_factory=lambda: _env("ML_URL", "http://ml-service:8001"))
    ml_timeout_s: float = field(default_factory=lambda: _env("ML_TIMEOUT_S", 1.5, float))
    ml_breaker_failures: int = field(default_factory=lambda: _env("ML_BREAKER_FAILURES", 3, int))
    ml_breaker_cooldown_s: float = field(default_factory=lambda: _env("ML_BREAKER_COOLDOWN_S", 5.0, float))
    ml_history_rows: int = field(default_factory=lambda: _env("ML_HISTORY_ROWS", 20, int))

    # --- цикл прогноза
    predict_interval_s: float = field(default_factory=lambda: _env("PREDICT_INTERVAL_S", 1.0, float))
    predict_min_interval_s: float = field(default_factory=lambda: _env("PREDICT_MIN_INTERVAL_S", 0.15, float))
    horizon_min_s: int = field(default_factory=lambda: _env("HORIZON_MIN_S", 600, int))    # T+10 мин
    horizon_max_s: int = field(default_factory=lambda: _env("HORIZON_MAX_S", 900, int))    # T+15 мин

    # --- сопоставление с расписанием
    stop_radius_m: float = field(default_factory=lambda: _env("STOP_RADIUS_M", 45.0, float))
    stop_lookahead: int = field(default_factory=lambda: _env("STOP_LOOKAHEAD", 25, int))
    max_early_s: int = field(default_factory=lambda: _env("MAX_EARLY_S", 480, int))
    max_late_s: int = field(default_factory=lambda: _env("MAX_LATE_S", 720, int))
    # способ оценки cur_dev_s, если ТС ещё не доехало до остановки с планом ≤ T:
    # lower_bound — max(T − план, последнее отклонение); last_detected — последнее детектированное отклонение
    cur_dev_mode: str = field(default_factory=lambda: _env("CUR_DEV_MODE", "last_detected"))
    standing_speed_kmh: float = field(default_factory=lambda: _env("STANDING_SPEED_KMH", 3.0, float))

    # --- риск
    late_threshold_s: float = field(default_factory=lambda: _env("LATE_THRESHOLD_S", 120.0, float))   # как target_class
    early_threshold_s: float = field(default_factory=lambda: _env("EARLY_THRESHOLD_S", -60.0, float))
    red_threshold_s: float = field(default_factory=lambda: _env("RED_THRESHOLD_S", 240.0, float))
    red_probability: float = field(default_factory=lambda: _env("RED_PROBABILITY", 0.75, float))
    default_error_scale_s: float = field(default_factory=lambda: _env("DEFAULT_ERROR_SCALE_S", 70.0, float))

    # --- надёжность / деградация
    link_timeout_s: float = field(default_factory=lambda: _env("LINK_TIMEOUT_S", 15.0, float))
    vehicle_stale_s: float = field(default_factory=lambda: _env("VEHICLE_STALE_S", 120.0, float))  # data-time
    unknown_unit_ttl_s: float = field(default_factory=lambda: _env("UNKNOWN_UNIT_TTL_S", 300.0, float))
    clock_freerun_max_s: float = field(default_factory=lambda: _env("CLOCK_FREERUN_MAX_S", 3 * 3600.0, float))

    # --- отображение
    display_tz_offset_h: int = field(default_factory=lambda: _env("DISPLAY_TZ_OFFSET_H", 3, int))  # МСК


settings = Settings()
