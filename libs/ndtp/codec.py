"""NDTP (Navigation Data Transfer Protocol) codec.

Реализация по dataset/docs/Emulator-and-Telematic-Packets-Specification.md:

    кадр = [ NPL 15 байт ][ NPH 10 байт ][ тело ]

* все поля little-endian, структуры packed;
* CRC-16/Modbus (poly 0xA001, init 0xFFFF) по NPH + телу, в NPL кладётся со свапнутыми байтами;
* тело NPH_SND_REALTIME — последовательность ячеек [type u8][number u8][payload].

Модуль без внешних зависимостей: используется и Backend-сервисом (приём/разбор потока),
и replay-фидером (кодирование CSV-телеметрии в NDTP).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# Константы протокола
# ---------------------------------------------------------------------------
NPL_SIGNATURE = 0x7E7E
NPL_HEADER_SIZE = 15
NPH_HEADER_SIZE = 10
NPL_TYPE_NPH = 0x02
MAX_DATA_SIZE = 65535

# NPH service / type
NPH_SRV_GENERIC_CONTROLS = 0
NPH_SRV_NAVDATA = 1

NPH_RESULT = 0
NPH_SGC_CONN_REQUEST = 100
NPH_SND_REALTIME = 101
NPH_SND_HISTORY = 100  # в сервисе NAVDATA тип 100 — исторические данные (обрабатываем так же)

NPH_FLAG_REQUEST = 0x0001

_NPL = struct.Struct("<HHHHBIH")          # 15 байт
_NPH = struct.Struct("<HHHI")             # 10 байт
_HANDSHAKE = struct.Struct("<HHHIII")     # 18 байт
_RESULT = struct.Struct("<I")


def crc16_modbus(data: bytes) -> int:
    """CRC-16/Modbus: poly 0xA001 (reflected 0x8005), init 0xFFFF."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


# Табличная версия — в ~10 раз быстрее, используется на горячем пути.
_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xA001 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)


def crc16_modbus_fast(data: bytes) -> int:
    crc = 0xFFFF
    tbl = _CRC_TABLE
    for b in data:
        crc = (crc >> 8) ^ tbl[(crc ^ b) & 0xFF]
    return crc


def _swap16(v: int) -> int:
    return ((v & 0xFF) << 8) | (v >> 8)


# ---------------------------------------------------------------------------
# Ячейки телематики: (type) -> (имя, struct, поля)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CellSpec:
    type_id: int
    name: str
    fmt: struct.Struct
    fields: tuple[str, ...]

    @property
    def size(self) -> int:
        return self.fmt.size


def _spec(type_id: int, name: str, fmt: str, fields: list[str]) -> CellSpec:
    return CellSpec(type_id, name, struct.Struct("<" + fmt), tuple(fields))


CELL_SPECS: dict[int, CellSpec] = {
    s.type_id: s
    for s in [
        # 6.1 навигация, 26 байт
        _spec(0, "G6CellNav00", "IIIBBHHHHHBB", [
            "timestamp", "longitude", "latitude", "extraDop", "batVoltage",
            "speedAvg", "speedMax", "course", "track", "altitude", "nsat", "pdop",
        ]),
        # 6.2 внутренние датчики, 26 байт
        _spec(2, "G6CellIntSensor02", "HHHHBBHHHHIBBBb", [
            "an_in0", "an_in1", "an_in2", "an_in3", "di_in", "di_out",
            "di0_counter", "di1_counter", "di2_counter", "di3_counter",
            "odometer", "csq", "gprs_state", "accel_energy", "ext_volt",
        ]),
        # 6.3 ДУТ УЗИ-M, 6 байт
        _spec(8, "G6CellUsi08", "BHHB", ["det_status", "level_mm", "level_l", "temperature"]),
        # 6.4 CAN, 37 байт
        _spec(10, "G6CellCan10", "IIIIHHhB5HI", [
            "secFlagStatus", "allTimeEngine", "allTrack", "allFuelConsum", "fuelLevel",
            "speedTurnEngine", "tEngine", "speed",
            "pressureAxis1", "pressureAxis2", "pressureAxis3", "pressureAxis4", "pressureAxis5",
            "flagAlarm",
        ]),
        # 6.6 LLS, 50 байт
        _spec(15, "G6CellLls15", "H12I", [
            "status", "main_float_level", "temperature_average", "percent_of_volume",
            "total_Volume", "weight", "density", "net_Standard_Volume", "level_of_water",
            "pressure", "vapor_temperature_average", "vapor_Weight", "liquid_phase_Weight",
        ]),
        # 6.5 температура, 8 байт
        _spec(16, "G6CellTermo16", "Ii", ["status", "temp"]),
    ]
}


# ---------------------------------------------------------------------------
# Структуры результата разбора
# ---------------------------------------------------------------------------
@dataclass
class NavRecord:
    """Раскодированная G6CellNav00 в терминах traffic.csv."""
    timestamp: int                # Unix-секунды (UTC)
    lon: float | None
    lat: float | None
    location_valid: bool
    speed: float                  # speedAvg, км/ч
    speed_max: float
    heading: float                # course, градусы
    alt: float
    nsat: int
    pdop: int
    bat_voltage_v: float
    track_m: int
    alarm: bool
    sos: bool

    @classmethod
    def from_cell(cls, c: dict[str, Any]) -> "NavRecord":
        bits = c["extraDop"]
        lat_north = bool(bits & (1 << 5))
        lon_east = bool(bits & (1 << 6))
        valid = bool(bits & (1 << 7))
        lon = c["longitude"] / 1e7 * (1 if lon_east else -1)
        lat = c["latitude"] / 1e7 * (1 if lat_north else -1)
        if c["longitude"] == 0 and c["latitude"] == 0:
            lon = lat = None
            valid = False
        return cls(
            timestamp=c["timestamp"],
            lon=lon,
            lat=lat,
            location_valid=valid,
            speed=float(c["speedAvg"]),
            speed_max=float(c["speedMax"]),
            heading=float(c["course"]),
            alt=float(c["altitude"]),
            nsat=c["nsat"],
            pdop=c["pdop"],
            bat_voltage_v=c["batVoltage"] * 0.02,
            track_m=c["track"],
            alarm=bool(bits & (1 << 1)),
            sos=bool(bits & (1 << 2)),
        )


@dataclass
class Frame:
    peer_address: int             # unitId из NPL
    npl_request_id: int
    service_id: int
    nph_type: int
    nph_flags: int
    nph_request_id: int
    body: bytes
    cells: list[dict[str, Any]] = field(default_factory=list)
    handshake: dict[str, Any] | None = None
    unparsed_cells: int = 0       # ячейки неизвестного типа (разбор хвоста остановлен)

    @property
    def is_handshake(self) -> bool:
        return self.service_id == NPH_SRV_GENERIC_CONTROLS and self.nph_type == NPH_SGC_CONN_REQUEST

    @property
    def is_telemetry(self) -> bool:
        return self.service_id == NPH_SRV_NAVDATA and self.nph_type in (NPH_SND_REALTIME, NPH_SND_HISTORY)

    @property
    def is_history(self) -> bool:
        return self.service_id == NPH_SRV_NAVDATA and self.nph_type == NPH_SND_HISTORY

    @property
    def needs_reply(self) -> bool:
        return bool(self.nph_flags & NPH_FLAG_REQUEST)

    def nav(self) -> NavRecord | None:
        for c in self.cells:
            if c["type"] == 0:
                return NavRecord.from_cell(c)
        return None


class NDTPError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------
def parse_cells(body: bytes) -> tuple[list[dict[str, Any]], int]:
    """Разбирает тело realtime-пакета. Возвращает (ячейки, число неразобранных)."""
    cells: list[dict[str, Any]] = []
    pos = 0
    n = len(body)
    while pos + 2 <= n:
        ctype, number = body[pos], body[pos + 1]
        spec = CELL_SPECS.get(ctype)
        if spec is None and ctype in OPAQUE_CELL_SIZES:
            name, size = OPAQUE_CELL_SIZES[ctype]
            if pos + 2 + size > n:
                return cells, 1
            cells.append({"type": ctype, "number": number, "name": name,
                          "raw": body[pos + 2:pos + 2 + size].hex()})
            pos += 2 + size
            continue
        if spec is None or pos + 2 + spec.size > n:
            # Размер неизвестного типа нам не известен → дальше разбирать нельзя.
            return cells, 1
        values = spec.fmt.unpack_from(body, pos + 2)
        cell = dict(zip(spec.fields, values))
        cell["type"] = ctype
        cell["number"] = number
        cell["name"] = spec.name
        cells.append(cell)
        pos += 2 + spec.size
    return cells, 0


# Ячейки из §6.7: бинарный layout полей в спецификации не описан, но размер
# измерен на живом эмуляторе (ndtp-telemetry-emulator:1.0). Такие ячейки
# пропускаем целиком (payload отдаём как hex), не теряя последующие ячейки пакета.
OPAQUE_CELL_SIZES: dict[int, tuple[str, int]] = {
    3: ("G6CellCrown03", 14), 4: ("G6CellIrma04", 15), 5: ("G6CellKdm05", 6),
    6: ("G6CellIdn06", 9), 7: ("G6CellIdn07", 1), 9: ("G6CellReg09", 40),
    12: ("G6CellRfid12", 5), 13: ("G6CellPlo13", 13), 14: ("G6CellBms14", 15),
    17: ("G6CellAlcohol1st17", 46), 18: ("G6CellCAN18", 50), 19: ("G6CellGSMstations19", 40),
    20: ("G6CellM333CAN20", 8), 21: ("G6CellAlcohol2nd21", 180), 22: ("G6CellServerStatistics22", 24),
    23: ("G6CellTrackerStatistics23", 16), 100: ("G6CellZipSensorData100", 44),
}


def parse_frame(buf: bytes | bytearray | memoryview, verify_crc: bool = True) -> Frame:
    """Разбирает ровно один полный кадр."""
    if len(buf) < NPL_HEADER_SIZE + NPH_HEADER_SIZE:
        raise NDTPError("frame too short")
    sig, data_size, _flags, crc, npl_type, peer, npl_req = _NPL.unpack_from(buf, 0)
    if sig != NPL_SIGNATURE:
        raise NDTPError(f"bad signature 0x{sig:04X}")
    if npl_type != NPL_TYPE_NPH:
        raise NDTPError(f"unsupported NPL type {npl_type}")
    payload = bytes(buf[NPL_HEADER_SIZE:NPL_HEADER_SIZE + data_size])
    if len(payload) != data_size:
        raise NDTPError("truncated frame")
    if verify_crc:
        calc = crc16_modbus_fast(payload)
        # По спецификации CRC лежит со свапнутыми байтами; принимаем и «прямой» порядок
        # для совместимости с иными реализациями терминалов.
        if crc != _swap16(calc) and crc != calc:
            raise NDTPError(f"crc mismatch: got 0x{crc:04X}, calc 0x{calc:04X}")
    srv, nph_type, nph_flags, nph_req = _NPH.unpack_from(payload, 0)
    body = payload[NPH_HEADER_SIZE:]
    fr = Frame(peer, npl_req, srv, nph_type, nph_flags, nph_req, body)
    if fr.is_handshake and len(body) >= _HANDSHAKE.size:
        hi, lo, hflags, hpeer, max_size, _res = _HANDSHAKE.unpack_from(body, 0)
        fr.handshake = {
            "proto_version": f"{hi}.{lo}", "flags": hflags,
            "peer_address": hpeer, "max_packet_size": max_size,
        }
    elif fr.is_telemetry:
        fr.cells, fr.unparsed_cells = parse_cells(body)
    return fr


class StreamDecoder:
    """Инкрементальный декодер TCP-потока: накапливает байты, отдаёт целые кадры.

    Устойчив к мусору и битым кадрам: при ошибке сигнатуры/CRC пересинхронизируется
    по следующей сигнатуре 0x7E7E, не теряя соединение.
    """

    SIG = b"\x7e\x7e"

    def __init__(self, verify_crc: bool = True, max_buffer: int = 1 << 20):
        self._buf = bytearray()
        self.verify_crc = verify_crc
        self.max_buffer = max_buffer
        self.errors = 0
        self.skipped_bytes = 0          # мусор между кадрами, отброшенный при пересинхронизации
        self.last_error: str | None = None

    def feed(self, data: bytes) -> Iterator[Frame]:
        self._buf += data
        if len(self._buf) > self.max_buffer:  # защита от раздувания буфера
            self._buf = self._buf[-NPL_HEADER_SIZE:]
            self.errors += 1
            self.last_error = "buffer overflow"
        while True:
            start = self._buf.find(self.SIG)
            if start < 0:
                # оставляем последний байт — он может быть началом сигнатуры
                self.skipped_bytes += max(0, len(self._buf) - 1)
                del self._buf[:-1]
                return
            if start > 0:
                self.skipped_bytes += start
                del self._buf[:start]
            if len(self._buf) < NPL_HEADER_SIZE:
                return
            data_size = int.from_bytes(self._buf[2:4], "little")
            # валидируем заголовок ДО ожидания тела: ложная сигнатура 0x7E7E в мусоре
            # с большим dataSize иначе «подвесила» бы поток до 64 КБ
            if data_size < NPH_HEADER_SIZE or self._buf[8] != NPL_TYPE_NPH:
                self._skip("bad NPL header")
                continue
            total = NPL_HEADER_SIZE + data_size
            if len(self._buf) < total:
                return
            try:
                frame = parse_frame(self._buf[:total], self.verify_crc)
            except NDTPError as e:
                self._skip(str(e))
                continue
            del self._buf[:total]
            yield frame

    def _skip(self, reason: str) -> None:
        self.errors += 1
        self.last_error = reason
        del self._buf[:2]  # сдвигаемся за сигнатуру и ищем следующую


# ---------------------------------------------------------------------------
# Кодирование (для эмуляции терминала и ответов сервера)
# ---------------------------------------------------------------------------
def build_frame(peer_address: int, service_id: int, nph_type: int, request_id: int,
                body: bytes, nph_flags: int = NPH_FLAG_REQUEST, npl_request_id: int = 0) -> bytes:
    payload = _NPH.pack(service_id, nph_type, nph_flags, request_id & 0xFFFFFFFF) + body
    crc = _swap16(crc16_modbus_fast(payload))
    npl = _NPL.pack(NPL_SIGNATURE, len(payload), 0, crc, NPL_TYPE_NPH,
                    peer_address & 0xFFFFFFFF, npl_request_id & 0xFFFF)
    return npl + payload


def build_handshake(unit_id: int, request_id: int) -> bytes:
    body = _HANDSHAKE.pack(6, 2, 0, unit_id, 65535, 0)
    return build_frame(unit_id, NPH_SRV_GENERIC_CONTROLS, NPH_SGC_CONN_REQUEST, request_id, body)


def build_result(to_frame: Frame, error: int = 0) -> bytes:
    """Ответ сервера NPH_RESULT на запрос (flags.request=1)."""
    return build_frame(to_frame.peer_address, to_frame.service_id, NPH_RESULT,
                       to_frame.nph_request_id, _RESULT.pack(error), nph_flags=0)


def encode_cell(type_id: int, number: int, values: dict[str, Any]) -> bytes:
    spec = CELL_SPECS[type_id]
    return bytes((type_id, number)) + spec.fmt.pack(*(int(values.get(f, 0)) for f in spec.fields))


def encode_nav(timestamp: int, lon: float | None, lat: float | None, valid: bool,
               speed: float = 0.0, heading: float = 0.0, alt: float = 0.0,
               nsat: int = 12, pdop: int = 10, bat_voltage_v: float = 4.1,
               track_m: int = 0) -> bytes:
    """G6CellNav00 из полей traffic.csv (обратное преобразование к таблице §8 README)."""
    has_pos = lon is not None and lat is not None
    bits = 0
    if not has_pos or lat >= 0:
        bits |= 1 << 5
    if not has_pos or lon >= 0:
        bits |= 1 << 6
    if valid and has_pos:
        bits |= 1 << 7
    spd = max(0, min(65535, int(round(speed or 0))))
    return encode_cell(0, 0, {
        "timestamp": int(timestamp),
        "longitude": int(round(abs(lon) * 1e7)) if has_pos else 0,
        "latitude": int(round(abs(lat) * 1e7)) if has_pos else 0,
        "extraDop": bits,
        "batVoltage": max(0, min(255, int(bat_voltage_v / 0.02))),
        "speedAvg": spd,
        "speedMax": spd,
        "course": max(0, min(65535, int(round(heading or 0)))) % 361,
        "track": int(track_m) % 65535,
        "altitude": max(0, min(65535, int(round(alt or 0)))),
        "nsat": nsat,
        "pdop": pdop,
    })


def build_realtime(unit_id: int, request_id: int, cells: bytes, history: bool = False) -> bytes:
    return build_frame(unit_id, NPH_SRV_NAVDATA,
                       NPH_SND_HISTORY if history else NPH_SND_REALTIME, request_id, cells)
