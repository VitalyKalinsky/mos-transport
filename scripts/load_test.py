"""Нагрузочный тест приёма NDTP: N синтетических терминалов шлют пакеты в Backend.

Запуск (стек поднят):  python scripts/load_test.py --units 1000 --rate 1 --duration 60
Проверяет: пропускную способность приёма, отсутствие накопления очередей (backend получает
столько же, сколько отправлено), время обработки пакета, отзывчивость API под нагрузкой.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs"))
import ndtp  # noqa: E402


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


async def unit(uid: int, host: str, port: int, rate: float, stop: float, stats: dict) -> None:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError:
        stats["conn_err"] += 1
        return
    writer.write(ndtp.build_handshake(uid, 1))
    await writer.drain()

    async def drain():
        try:
            while await reader.read(65536):
                pass
        except Exception:  # noqa: BLE001
            pass
    asyncio.create_task(drain())
    lon, lat, req = 37.4 + random.random() * 0.4, 55.6 + random.random() * 0.3, 1
    await asyncio.sleep(random.random() / rate)
    try:
        while time.time() < stop:
            req += 1
            lon += random.uniform(-1e-4, 1e-4)
            lat += random.uniform(-1e-4, 1e-4)
            cell = ndtp.encode_nav(int(time.time()), lon, lat, True, speed=random.uniform(0, 50), heading=random.uniform(0, 359))
            writer.write(ndtp.build_realtime(uid, req, cell))
            await writer.drain()
            stats["sent"] += 1
            await asyncio.sleep(1 / rate)
    except OSError:
        stats["conn_err"] += 1
    finally:
        writer.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=9201)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--units", type=int, default=1000)
    ap.add_argument("--rate", type=float, default=1.0, help="пакетов/с на терминал")
    ap.add_argument("--duration", type=float, default=60)
    a = ap.parse_args()

    before = get(a.api + "/api/metrics")["ingest"]["packets_total"]
    stats = {"sent": 0, "conn_err": 0}
    stop = time.time() + a.duration
    tasks = [asyncio.create_task(unit(9_000_000 + i, a.host, a.port, a.rate, stop, stats)) for i in range(a.units)]

    api_lat = []
    while time.time() < stop:
        await asyncio.sleep(2)
        t0 = time.perf_counter()
        await asyncio.to_thread(get, a.api + "/health")
        api_lat.append((time.perf_counter() - t0) * 1000)
    await asyncio.gather(*tasks)
    await asyncio.sleep(2)
    m = get(a.api + "/api/metrics")
    received = m["ingest"]["packets_total"] - before
    api_lat.sort()
    print(f"units={a.units} rate/unit={a.rate}/s duration={a.duration}s  conn_errors={stats['conn_err']}")
    print(f"sent={stats['sent']}  received_by_backend={received}  loss={(1 - received / max(1, stats['sent'])):.2%}")
    print(f"throughput ≈ {received / a.duration:.0f} packets/s")
    print(f"ingest processing per packet (µs): {m['ingest']['processing_us']}")
    print(f"prediction cycle (ms): {m['prediction']['cycle_ms']}  overruns={m['prediction']['cycle_overruns']}")
    print(f"API /health latency under load: p50={api_lat[len(api_lat) // 2]:.1f} ms  max={api_lat[-1]:.1f} ms")
    print(f"parse_errors={m['ndtp']['parse_errors']} handler_errors={m['ndtp']['handler_errors']}")


if __name__ == "__main__":
    asyncio.run(main())
