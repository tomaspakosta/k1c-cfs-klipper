"""
Tests for cfs_protocol.py's framing/CRC8 against real captured traffic.

The vectors below are copied from ityshchenko/klipper-cfs's test suite
(tests/test_structures.py), which captured them with interceptty from a
real printer talking to a real CFS box during multi-color printing. They
are independent of anything we built - if our crc8()/build_frame() don't
reproduce these exactly, our framing is wrong.

Run with: pytest tests/test_protocol.py -v
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cfs_protocol import crc8, build_frame  # noqa: E402


# (slave_addr, status, function_code, data, expected_full_frame_hex)
REAL_CAPTURED_VECTORS = [
    (0x01, 0x00, 0xA3, b"", "f7010300a3dd"),
    (0x02, 0x00, 0xA3, b"", "f7020300a3dd"),
    (0x03, 0x00, 0xA3, b"", "f7030300a3dd"),
    (0x04, 0x00, 0xA3, b"", "f7040300a3dd"),
    (0x01, 0xFF, 0x04, bytes([0x00, 0x01]), "f70105ff04000190"),
    (0x01, 0xFF, 0x14, b"", "f70103ff1406"),
    (0x01, 0xFF, 0x0D, bytes([0x0F, 0x01]), "f70105ff0d0f0169"),
    (0x01, 0xFF, 0x0A, b"", "f70103ff0a5c"),
    (0xFE, 0x00, 0xA1, bytes([0xFE, 0xFE]), "f7fe0500a1fefef8"),
    (0x01, 0x00, 0xA2, b"", "f7010300a2da"),
]

# Our own live-captured vectors from testing against real hardware
# (see docs/PROTOCOL.md for the full story these came from)
OUR_LIVE_VECTORS = [
    # broadcast discovery request
    (0xFE, 0x00, 0xA1, bytes([0xFE, 0xFE]), "f7fe0500a1fefef8"),
    # CTRL_CONNECTION_MOTOR_ACTION, ACTION=EXTRUDE
    (0x01, 0xFF, 0x07, bytes([0x01]), "f70104ff07011f"),
    # CTRL_CONNECTION_MOTOR_ACTION, ACTION=STOP
    (0x01, 0xFF, 0x07, bytes([0x00]), "f70104ff070018"),
    # TIGHTEN_UP_ENABLE, enable
    (0x01, 0xFF, 0x0F, bytes([0x01]), "f70104ff0f01b7"),
    # RETRUDE_PROCESS slot A, stage 0
    (0x01, 0xFF, 0x11, bytes([0x01, 0x00]), "f70105ff110100e0"),
]


def test_real_captured_vectors():
    for slave_addr, status, fn, data, expected_hex in REAL_CAPTURED_VECTORS:
        frame = build_frame(slave_addr, status, fn, data)
        assert frame.hex() == expected_hex, (
            f"build_frame({slave_addr:#04x}, {status:#04x}, {fn:#04x}, {data!r}) "
            f"= {frame.hex()}, expected {expected_hex}"
        )


def test_our_live_vectors():
    for slave_addr, status, fn, data, expected_hex in OUR_LIVE_VECTORS:
        frame = build_frame(slave_addr, status, fn, data)
        assert frame.hex() == expected_hex, (
            f"build_frame({slave_addr:#04x}, {status:#04x}, {fn:#04x}, {data!r}) "
            f"= {frame.hex()}, expected {expected_hex}"
        )


def test_crc8_known_values():
    # crc8 is computed over [length, status, fn, *data] - spot check a
    # couple of these directly, not just via build_frame.
    assert crc8(bytes([0x05, 0x00, 0xA1, 0xFE, 0xFE])) == 0xF8
    assert crc8(bytes([0x03, 0x00, 0xA3])) == 0xDD


def test_length_byte_matches_payload():
    # length must equal status+fn+data+crc, i.e. len(frame) - 3
    for data_len in range(0, 20):
        frame = build_frame(0x01, 0xFF, 0x10, bytes(data_len))
        length_byte = frame[2]
        assert length_byte == len(frame) - 3, (
            f"data_len={data_len}: length byte {length_byte} != {len(frame) - 3}"
        )
