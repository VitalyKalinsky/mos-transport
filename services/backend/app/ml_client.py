"""HTTP-клиент ML-сервиса с circuit breaker и деградацией на baseline-прогноз."""
from __future__ import annotations

import logging
import time

import httpx

from .config import Settings
from .metrics import LatencyWindow

log = logging.getLogger("backend.ml")


class MLClient:
    """Клиент ``/v1/predict`` и ``/v1/model`` ML-сервиса.

    После ``ml_breaker_failures`` ошибок подряд breaker размыкается на ``ml_breaker_cooldown_s``:
    цикл прогноза сразу идёт в fallback и не ждёт таймаут на каждой итерации.

    Args:
        cfg: Настройки сервиса.

    Attributes:
        available (bool): Последний вызов ML прошёл успешно.
        error_scale_s (float): Масштаб ошибки прогноза (валидационная MAE модели), с; для P(late).
        fallbacks (int): Сколько раз прогноз ушёл в baseline.
    """

    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        # один AsyncClient на процесс: keep-alive, без TCP-handshake на каждый батч
        self._client = httpx.AsyncClient(base_url=cfg.ml_url, timeout=cfg.ml_timeout_s)
        self.failures = 0
        self.open_until = 0.0
        self.available = False
        self.last_error: str | None = None
        self.error_scale_s = cfg.default_error_scale_s
        self.model_info: dict | None = None
        self.latency = LatencyWindow()
        self.calls = 0
        self.fallbacks = 0

    async def close(self) -> None:
        """Закрывает HTTP-соединения."""
        await self._client.aclose()

    @property
    def breaker_open(self) -> bool:
        """bool: Breaker разомкнут — ML не вызывается до истечения паузы."""
        return time.time() < self.open_until

    async def refresh_info(self) -> None:
        """Обновляет метаданные модели (``/v1/model``). Ошибки не пробрасываются: только ``last_error``."""
        try:
            r = await self._client.get("/v1/model")
            r.raise_for_status()
            self.model_info = r.json()
            mae = self.model_info.get("validation_mae_s")
            if mae:
                self.error_scale_s = float(mae)
            self.available = bool(self.model_info.get("ready"))
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self.last_error = f"info: {e}"

    async def predict(self, points: list[dict], telemetry: list[dict]) -> list[dict] | None:
        """Батч-прогноз задержки на целевых остановках.

        Args:
            points: Точки прогноза (``sample_id``, ``tr_id``, ``T``, ``target_stop_id``,
                ``target_time_begin``, ``cur_dev_s``).
            telemetry: Последние пакеты телеметрии по этим ТС (строки формата traffic.csv).

        Returns:
            list[dict] | None: Прогнозы в порядке ``points``; ``None`` — ML недоступен
            (ошибка, таймаут или разомкнутый breaker), вызывающий переходит на baseline.
            Исключения не пробрасываются.
        """
        if not points:
            return []
        if self.breaker_open:
            self.fallbacks += 1
            return None
        t0 = time.perf_counter()
        try:
            r = await self._client.post("/v1/predict", json={"points": points, "telemetry": telemetry})
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001 — любой сбой ML = деградация, а не падение цикла
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {e}"
            self.available = False
            if self.failures >= self.cfg.ml_breaker_failures:
                self.open_until = time.time() + self.cfg.ml_breaker_cooldown_s
                log.warning("ML circuit breaker OPEN for %.0fs: %s", self.cfg.ml_breaker_cooldown_s, self.last_error)
            self.fallbacks += 1
            return None
        self.latency.add((time.perf_counter() - t0) * 1000)
        self.calls += 1
        if not self.available or self.failures:
            log.info("ML service available")
        self.failures = 0
        self.available = True
        self.last_error = None
        mae = data.get("model_validation_mae_s")
        if mae:  # модель могли дообучить и перезапустить — масштаб ошибки берём из каждого ответа
            self.error_scale_s = float(mae)
        return data["predictions"]
