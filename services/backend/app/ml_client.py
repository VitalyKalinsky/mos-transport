"""Клиент ML-сервиса с circuit breaker и деградацией на baseline-прогноз."""
from __future__ import annotations

import logging
import time

import httpx

from .config import Settings
from .metrics import LatencyWindow

log = logging.getLogger("backend.ml")


class MLClient:
    def __init__(self, cfg: Settings) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(base_url=cfg.ml_url, timeout=cfg.ml_timeout_s)
        self.failures = 0
        self.open_until = 0.0
        self.available = False
        self.last_error: str | None = None
        self.error_scale_s = cfg.default_error_scale_s   # MAE модели → масштаб ошибки
        self.model_info: dict | None = None
        self.latency = LatencyWindow()
        self.calls = 0
        self.fallbacks = 0

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def breaker_open(self) -> bool:
        return time.time() < self.open_until

    async def refresh_info(self) -> None:
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
        """None → ML недоступен (вызывающий переключается на fallback)."""
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
        except Exception as e:  # noqa: BLE001
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
        if mae:
            self.error_scale_s = float(mae)
        return data["predictions"]
