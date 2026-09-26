"""TCP-сервер приёма NDTP (роль «NDTP-сервера» для эмулятора / бортовых терминалов).

* Каждое соединение обслуживается отдельной корутиной; ошибка в одном соединении
  не влияет на остальные и на сервис в целом.
* Handshake (NPH_SGC_CONN_REQUEST) и пакеты с флагом request подтверждаются NPH_RESULT.
* Битые кадры (CRC/сигнатура) пропускаются с пересинхронизацией, соединение не рвётся.
* Простаивающее соединение закрывается по таймауту; терминал сам переподключается.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import ndtp

log = logging.getLogger("backend.ndtp")


@dataclass
class ConnInfo:
    conn_id: int
    peer: str
    connected_at: float = field(default_factory=time.time)
    unit_id: int | None = None
    frames: int = 0
    bytes: int = 0
    errors: int = 0
    last_frame_at: float | None = None
    proto_version: str | None = None


class NDTPServer:
    def __init__(self, host: str, port: int, on_frame: Callable[[ndtp.Frame, ConnInfo, float], None],
                 verify_crc: bool = True, idle_timeout_s: float = 120.0) -> None:
        self.host, self.port = host, port
        self.on_frame = on_frame
        self.verify_crc = verify_crc
        self.idle_timeout_s = idle_timeout_s
        self.connections: dict[int, ConnInfo] = {}
        self.total_connections = 0
        self.total_disconnects = 0
        self.parse_errors = 0
        self.skipped_bytes = 0
        self.handler_errors = 0
        self._server: asyncio.base_events.Server | None = None
        self._seq = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=1 << 20)
        log.info("NDTP server listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._seq += 1
        peer = writer.get_extra_info("peername")
        info = ConnInfo(self._seq, f"{peer[0]}:{peer[1]}" if peer else "?")
        self.connections[info.conn_id] = info
        self.total_connections += 1
        decoder = ndtp.StreamDecoder(verify_crc=self.verify_crc)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(65536), timeout=self.idle_timeout_s)
                except asyncio.TimeoutError:
                    log.info("conn %d (unit %s) idle timeout", info.conn_id, info.unit_id)
                    break
                if not data:
                    break
                rx = time.time()
                info.bytes += len(data)
                errors_before, skipped_before = decoder.errors, decoder.skipped_bytes
                replies: list[bytes] = []
                for frame in decoder.feed(data):
                    info.frames += 1
                    info.last_frame_at = rx
                    if info.unit_id is None or frame.is_handshake:
                        info.unit_id = frame.peer_address
                    if frame.handshake:
                        info.proto_version = frame.handshake["proto_version"]
                    try:
                        self.on_frame(frame, info, rx)
                    except Exception:  # noqa: BLE001 — ошибка обработки не рвёт соединение
                        self.handler_errors += 1
                        log.exception("frame handler failed (unit %s)", frame.peer_address)
                    if frame.needs_reply:
                        replies.append(ndtp.build_result(frame, 0))
                self.skipped_bytes += decoder.skipped_bytes - skipped_before
                new_err = decoder.errors - errors_before
                if new_err:
                    info.errors += new_err
                    self.parse_errors += new_err
                    log.warning("conn %d: %d bad frame(s): %s", info.conn_id, new_err, decoder.last_error)
                if replies:
                    writer.write(b"".join(replies))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            log.info("conn %d (unit %s) dropped: %s", info.conn_id, info.unit_id, e)
        finally:
            self.connections.pop(info.conn_id, None)
            self.total_disconnects += 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
