"""Тесты NDTP-кодека. Фикстуры — реальные TCP-захваты ndtp-telemetry-emulator:1.0."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ndtp  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def frames(name: str) -> list[ndtp.Frame]:
    dec = ndtp.StreamDecoder()
    out = list(dec.feed((FIX / name).read_bytes()))
    assert dec.errors == 0
    return out


def test_crc16_modbus_reference_vector():
    # эталон CRC-16/MODBUS для "123456789"
    assert ndtp.crc16_modbus(b"123456789") == 0x4B37
    assert ndtp.crc16_modbus_fast(b"123456789") == 0x4B37


def test_emulator_capture_handshake_and_realtime():
    fr = frames("emulator_capture_1.bin")
    hs = [f for f in fr if f.is_handshake]
    rt = [f for f in fr if f.is_telemetry]
    assert hs and rt
    assert hs[0].handshake == {"proto_version": "6.2", "flags": 0, "peer_address": hs[0].peer_address,
                               "max_packet_size": 65535}
    auto = [f for f in rt if f.peer_address == 1166336]
    names = [c["name"] for c in auto[0].cells]
    # autoGenerate + пустой cells → Nav00 первой, затем стандартный набор (§3.2)
    assert names[0] == "G6CellNav00"
    assert set(names) == {"G6CellNav00", "G6CellUsi08", "G6CellTermo16", "G6CellIntSensor02", "G6CellCan10"}
    nav = auto[0].nav()
    assert nav.location_valid and 55 < nav.lat < 56.5 and 37 < nav.lon < 38.5
    assert all(f.needs_reply for f in fr)


def test_emulator_capture_all_opaque_cells_are_skipped_correctly():
    for name in ("emulator_capture_1.bin", "emulator_capture_2.bin"):
        for f in frames(name):
            if f.is_telemetry:
                assert f.unparsed_cells == 0, f.cells
                assert f.cells[0]["name"] == "G6CellNav00"
    rt = [f for f in frames("emulator_capture_1.bin") if f.peer_address == 777 and f.is_telemetry][0]
    assert [c["type"] for c in rt.cells] == [0, 3, 5, 6, 7, 9, 12, 13, 17, 18, 19, 20, 22, 23, 16]


@pytest.mark.parametrize("lon,lat,valid", [(37.6173210, 55.7551234, True), (-73.9857, 40.7484, True),
                                           (151.2093, -33.8688, True), (None, None, False)])
def test_nav_roundtrip(lon, lat, valid):
    cell = ndtp.encode_nav(1767690000, lon, lat, valid, speed=37, heading=271, alt=160)
    raw = ndtp.build_realtime(1234567, 42, cell)
    (f,) = list(ndtp.StreamDecoder().feed(raw))
    nav = f.nav()
    assert f.peer_address == 1234567 and f.nph_request_id == 42 and f.is_telemetry
    assert nav.timestamp == 1767690000 and nav.location_valid == valid
    if valid:
        assert nav.lon == pytest.approx(lon, abs=1e-7) and nav.lat == pytest.approx(lat, abs=1e-7)
        assert nav.speed == 37 and nav.heading == 271 and nav.alt == 160
    else:
        assert nav.lon is None and nav.lat is None


def test_crc_is_byte_swapped_in_npl():
    raw = ndtp.build_handshake(5, 1)
    crc_calc = ndtp.crc16_modbus(raw[15:])
    assert raw[6:8] == crc_calc.to_bytes(2, "big")   # swap(le) == big-endian


def test_stream_resync_after_garbage_and_bad_crc():
    good = ndtp.build_realtime(1, 1, ndtp.encode_nav(1, 37.5, 55.7, True))
    bad = bytearray(good)
    bad[-1] ^= 0xFF                                # порча тела → CRC mismatch
    stream = os.urandom(50) + bytes(bad) + b"\x7e\x7e\x01" + good + good
    dec = ndtp.StreamDecoder()
    out = []
    for i in range(0, len(stream), 7):             # подача мелкими TCP-кусками
        out += list(dec.feed(stream[i:i + 7]))
    assert len(out) == 2 and dec.errors >= 1


def test_result_reply_mirrors_request():
    (f,) = list(ndtp.StreamDecoder().feed(ndtp.build_handshake(77, 9)))
    (r,) = list(ndtp.StreamDecoder().feed(ndtp.build_result(f)))
    assert r.peer_address == 77 and r.nph_type == ndtp.NPH_RESULT and r.nph_request_id == 9
    assert not r.needs_reply
