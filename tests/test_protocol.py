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
from cfs_protocol import crc8, build_frame, find_cfs_port, decode_measuring_wheel  # noqa: E402


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


def test_find_cfs_port_falls_back_gracefully():
    # CI runners (and most dev machines) won't have a CH340 device
    # plugged in - this just confirms the fallback path never raises and
    # returns the fallback value unchanged. The real detection logic is
    # confirmed separately against actual hardware, not here.
    assert find_cfs_port(fallback="/dev/ttyUSB0") == "/dev/ttyUSB0"
    assert find_cfs_port(fallback="COM99") == "COM99"


def test_decode_measuring_wheel_matches_our_captured_extrude_telemetry():
    # Real EXTRUDE_PROCESS stage-5 telemetry bytes captured during the
    # toolhead-reach test (see docs/PROTOCOL.md). All should decode as
    # negative, with magnitude climbing while material was actively
    # feeding, then flattening out once the toolhead sensor triggered.
    samples_hex = [
        "c534534e", "c54d692a", "c563e7b9", "c57a8579", "c58898b7",
        "c593dcc5", "c5a07a66", "c5ac116e",
    ]
    settled_hex = ["c5afa071", "c5af9fe3", "c5af9f89"]

    decoded = [decode_measuring_wheel(bytes.fromhex(h)) for h in samples_hex]
    assert all(v < 0 for v in decoded), "all readings should be negative"
    # magnitude climbs while feeding -> the (negative) value itself decreases
    assert decoded == sorted(decoded, reverse=True), \
        "magnitude should climb (value should decrease) monotonically while feeding"

    settled = [decode_measuring_wheel(bytes.fromhex(h)) for h in settled_hex]
    assert max(settled) - min(settled) < 1.0, "settled readings should be within ~noise of each other"


def test_decode_measuring_wheel_wrong_length_returns_none():
    assert decode_measuring_wheel(b"\x00\x01\x02") is None
    assert decode_measuring_wheel(b"") is None


def test_length_byte_matches_payload():
    # length must equal status+fn+data+crc, i.e. len(frame) - 3
    for data_len in range(0, 20):
        frame = build_frame(0x01, 0xFF, 0x10, bytes(data_len))
        length_byte = frame[2]
        assert length_byte == len(frame) - 3, (
            f"data_len={data_len}: length byte {length_byte} != {len(frame) - 3}"
        )
