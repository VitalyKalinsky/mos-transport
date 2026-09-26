"""Replay-фидер: воспроизводит телеметрию датасета (traffic.csv) как живой NDTP-поток.

Ведёт себя как эмулятор ndtp-telemetry-emulator (§4 спецификации): на каждый unitId —
отдельное TCP-соединение, handshake NPH_SGC_CONN_REQUEST, пауза 200 мс, далее пакеты
NPH_SND_REALTIME с ячейкой G6CellNav00; при обрыве — reconnect и повторный handshake.
Отличие от штатного эмулятора: координаты/скорость/время берутся из реальной телеметрии,
поэтому поток сопоставим с эталонным расписанием (штатный эмулятор шлёт случайные данные).

Управление (HTTP :8090): пауза, скорость, перемотка, имитация обрыва связи, мусор в канале.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager

import ndtp
import pandas as pd
from fastapi import FastAPI, HTTPException

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("feeder")

TRAFFIC = os.getenv("REPLAY_TRAFFIC", "/data/validate/traffic.csv")
TARGET_HOST = os.getenv("TARGET_HOST", "backend")
TARGET_PORT = int(os.getenv("TARGET_PORT", "9201"))
START = os.getenv("REPLAY_START", "2026-01-06 04:30:00")      # UTC (= 07:30 МСК)
SPEED = float(os.getenv("REPLAY_SPEED", "10"))
LOOP = os.getenv("REPLAY_LOOP", "true").lower() == "true"
UNITS = os.getenv("REPLAY_UNITS", "")                           # пусто = все
AUTOSTART = os.getenv("REPLAY_AUTOSTART", "true").lower() == "true"


class Replay:
    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.units: list[int] = []
        self.data_start = 0.0
        self.data_end = 0.0
        self.speed = SPEED
        self.running = False
        self.anchor_data = 0.0
        self.anchor_wall = 0.0
        self.pos = 0
        self.queues: dict[int, asyncio.Queue] = {}
        self.conns: dict[int, dict] = {}
        self.outage_until = 0.0
        self.sent = 0
        self.dropped = 0
        self.loops = 0
        self._garbage: set[int] = set()

    # --------------------------------------------------------------- data
    def load(self) -> None:
        t = pd.read_csv(TRAFFIC, low_memory=False)
        if UNITS:
            t = t[t.unit_id.isin([int(u) for u in UNITS.split(",")])]
        ev = pd.to_datetime(t.event_time, format="ISO8601")
        rx = pd.to_datetime(t.receive_time, format="ISO8601")
        epoch = pd.Timestamp("1970-01-01")
        t = t.assign(ts=(ev - epoch).dt.total_seconds(), rx=(rx - epoch).dt.total_seconds())
        # порядок потока = порядок поступления на сервер
        t = t.sort_values(["ts", "rx"])
        self.rows = list(zip(
            t.unit_id.astype(int), t.ts, t.location_valid.astype(bool),
            t.lon, t.lat, t.alt, t.speed, t.heading, t.is_hist_data.astype(bool),
        ))
        self.units = sorted(t.unit_id.astype(int).unique().tolist())
        self.data_start = float(t.ts.min())
        self.data_end = float(t.ts.max())
        log.info("loaded %d rows, %d units, %s .. %s", len(self.rows), len(self.units),
                 pd.Timestamp(self.data_start, unit="s"), pd.Timestamp(self.data_end, unit="s"))

    def data_now(self) -> float:
        if not self.running:
            return self.anchor_data
        return self.anchor_data + (time.time() - self.anchor_wall) * self.speed

    def seek(self, data_ts: float) -> None:
        import bisect
        self.anchor_data = max(self.data_start, min(self.data_end, data_ts))
        self.anchor_wall = time.time()
        self.pos = bisect.bisect_left(self.rows, self.anchor_data, key=lambda r: r[1])

    def set_speed(self, speed: float) -> None:
        now = self.data_now()
        self.speed = speed
        self.anchor_data, self.anchor_wall = now, time.time()

    # ------------------------------------------------------------- pacing
    async def scheduler(self) -> None:
        while True:
            if not self.running:
                await asyncio.sleep(0.2)
                continue
            now = self.data_now()
            batch = 0
            while self.pos < len(self.rows) and self.rows[self.pos][1] <= now:
                r = self.rows[self.pos]
                q = self.queues.get(r[0])
                if q is not None:
                    if q.full():
                        self.dropped += 1
                    else:
                        q.put_nowait(r)
                self.pos += 1
                batch += 1
            if self.pos >= len(self.rows):
                if LOOP:
                    self.loops += 1
                    log.info("end of data — loop #%d from start", self.loops)
                    self.seek(pd.Timestamp(START).timestamp() if START else self.data_start)
                else:
                    self.running = False
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------- units
    async def unit_worker(self, unit: int) -> None:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.queues[unit] = q
        info = self.conns.setdefault(unit, {"connected": False, "reconnects": 0, "sent": 0, "last_error": None})
        req = 0
        backoff = 1.0
        writer = None
        while True:
            if time.time() < self.outage_until:
                # обрыв связи: соединения нет, данные теряются (как у штатного эмулятора)
                if writer is not None:
                    writer.close()
                    writer = None
                    info["connected"] = False
                while not q.empty():
                    q.get_nowait()
                    self.dropped += 1
                await asyncio.sleep(0.2)
                continue
            if writer is None:
                try:
                    reader, writer = await asyncio.wait_for(asyncio.open_connection(TARGET_HOST, TARGET_PORT), 5)
                    req += 1
                    writer.write(ndtp.build_handshake(unit, req))
                    await writer.drain()
                    await asyncio.sleep(0.2)
                    info.update(connected=True, last_error=None)
                    info["reconnects"] += 1
                    backoff = 1.0
                    asyncio.create_task(self._drain_replies(reader))
                except Exception as e:  # noqa: BLE001
                    info.update(connected=False, last_error=str(e))
                    writer = None
                    while not q.empty():
                        q.get_nowait()
                        self.dropped += 1
                    await asyncio.sleep(backoff)
                    backoff = min(10.0, backoff * 2)
                    continue
            try:
                r = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if unit in self._garbage:
                    self._garbage.discard(unit)
                    writer.write(os.urandom(37) + ndtp.build_handshake(unit, 0)[:-3] + b"\x00\x00\x00")
                continue
            _, ts, valid, lon, lat, alt, speed, heading, is_hist = r
            has = valid and lon == lon and lat == lat
            cell = ndtp.encode_nav(
                int(ts), lon if has else None, lat if has else None, has,
                speed=0.0 if speed != speed else speed, heading=0.0 if heading != heading else heading,
                alt=0.0 if alt != alt else alt,
            )
            req += 1
            try:
                if unit in self._garbage:
                    self._garbage.discard(unit)
                    writer.write(os.urandom(random.randint(5, 40)))
                writer.write(ndtp.build_realtime(unit, req, cell, history=is_hist))
                await writer.drain()
                self.sent += 1
                info["sent"] += 1
            except Exception as e:  # noqa: BLE001
                info.update(connected=False, last_error=str(e))
                self.dropped += 1
                writer = None

    @staticmethod
    async def _drain_replies(reader: asyncio.StreamReader) -> None:
        """Ответы сервера (NPH_RESULT) читаем и отбрасываем, как штатный эмулятор."""
        try:
            while await reader.read(4096):
                pass
        except Exception:  # noqa: BLE001
            pass

    def status(self) -> dict:
        return {
            "running": self.running, "speed": self.speed,
            "data_time_utc": str(pd.Timestamp(self.data_now(), unit="s")),
            "progress": round((self.data_now() - self.data_start) / max(1, self.data_end - self.data_start), 4),
            "target": f"{TARGET_HOST}:{TARGET_PORT}", "units": len(self.units),
            "connected_units": sum(1 for c in self.conns.values() if c["connected"]),
            "packets_sent": self.sent, "packets_dropped": self.dropped, "loops": self.loops,
            "outage_remaining_s": max(0.0, round(self.outage_until - time.time(), 1)),
        }


replay = Replay()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(replay.load)
    replay.seek(pd.Timestamp(START).timestamp() if START else replay.data_start)
    tasks = [asyncio.create_task(replay.scheduler())]
    tasks += [asyncio.create_task(replay.unit_worker(u)) for u in replay.units]
    replay.running = AUTOSTART
    replay.anchor_wall = time.time()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="NDTP Replay Feeder", version="1.0.0",
              description="Воспроизведение traffic.csv как NDTP-потока + сценарии отказов для демонстрации деградации.",
              lifespan=lifespan)


@app.get("/status")
def status():
    return replay.status()


@app.post("/pause")
def pause():
    replay.anchor_data = replay.data_now()
    replay.running = False
    return replay.status()


@app.post("/resume")
def resume():
    replay.anchor_wall = time.time()
    replay.running = True
    return replay.status()


@app.post("/speed/{value}")
def speed(value: float):
    if not 0.1 <= value <= 200:
        raise HTTPException(400, "speed must be within 0.1..200")
    replay.set_speed(value)
    return replay.status()


@app.post("/seek")
def seek(t: str):
    """Перемотка на время данных (UTC), например `2026-01-06 14:00:00`."""
    replay.seek(pd.Timestamp(t).timestamp())
    return replay.status()


@app.post("/outage/{seconds}")
def outage(seconds: float):
    """Имитация обрыва связи: все TCP-соединения закрываются на `seconds` секунд."""
    replay.outage_until = time.time() + max(0.0, min(seconds, 3600))
    return replay.status()


@app.post("/chaos/garbage")
def garbage():
    """Отправить в каждое соединение мусорные байты (проверка пересинхронизации парсера)."""
    replay._garbage.update(replay.units)
    return {"ok": True}
