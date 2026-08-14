"""
Minimal, dependency-light client for the Creality CFS (Color Filament
System / material box) RS-485-over-USB protocol.

This is NOT a Klipper extra — it's a small standalone library for talking
to a CFS box directly over its serial port, useful for exploration,
diagnostics, and as a reference for building a real Klipper integration.

Protocol frame:
    head(1)=0xF7  slave_addr(1)  length(1)  status(1)  function_code(1)  data(N)  crc8(1)

`length` counts everything *after* the length byte itself (status + fn + data + crc).
CRC8 (poly 0x07, no init/xorout) is computed over [length, status, fn, *data]
(i.e. everything between the length byte and the crc byte, inclusive of length).

See docs/PROTOCOL.md for the full command reference and how these values
were determined.
"""
from __future__ import annotations

import time
import serial


DEFAULT_BAUD = 230400

# Function codes (validated against real hardware + real captured traffic)
FN = {
    "GET_RFID": 0x02,
    "GET_REMAIN_LEN": 0x03,
    "SET_BOX_MODE": 0x04,
    "GET_BUFFER_STATE": 0x05,
    "CTRL_CONNECTION_MOTOR_ACTION": 0x07,
    "GET_FILAMENT_SENSOR_STATE": 0x08,
    "GET_BOX_STATE": 0x0A,
    "SET_PRE_LOADING": 0x0D,
    "TIGHTEN_UP_ENABLE": 0x0F,
    "EXTRUDE_PROCESS": 0x10,
    "RETRUDE_PROCESS": 0x11,
    "GET_VERSION_SN": 0x14,
    "GET_HARDWARE_STATUS": 0x15,
    # auto-addressing family
    "CMD_SET_SLAVE_ADDR": 0xA0,
    "CMD_GET_SLAVE_INFO": 0xA1,
    "CMD_ONLINE_CHECK": 0xA2,
    "CMD_GET_ADDR_TABLE": 0xA3,
}

# Slot bit values (bit-per-slot, physical left-to-right order).
# Empirically confirmed by pulling filament one slot at a time and watching
# GET_FILAMENT_SENSOR_STATE's bitmask clear one bit at a time.
SLOT_A = 0x01
SLOT_B = 0x02
SLOT_C = 0x04
SLOT_D = 0x08
SLOT_ALL = 0x0F

BROADCAST_ALL_BOXES = 0xFE


def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, no init/xorout. Matches the CFS wire format."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07 if crc & 0x80 else crc << 1) & 0xFF
    return crc


def build_frame(slave_addr: int, status: int, function_code: int, data: bytes = b"") -> bytes:
    length = 1 + 1 + len(data) + 1  # status + fn + data + crc
    body = bytes([length, status, function_code]) + data
    crc = crc8(body)
    return bytes([0xF7, slave_addr]) + body + bytes([crc])


class CFSClient:
    """Thin synchronous client. Not reactor-safe — do not use this directly
    inside a Klipper extra; see docs/PROTOCOL.md for notes on that."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD):
        self.ser = serial.Serial(port, baudrate=baud, timeout=1.0)

    def close(self):
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def send(self, slave_addr: int, status: int, function_code: int,
              data: bytes = b"", timeout: float = 2.0) -> bytes:
        """Send a frame and collect the response for up to `timeout` seconds.
        Returns the raw response bytes (possibly containing more than one
        frame if the box had queued replies — the box can be slow to reply
        during an active motor stage; don't assume one write == one frame)."""
        frame = build_frame(slave_addr, status, function_code, data)
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        deadline = time.time() + timeout
        total = b""
        while time.time() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                total += chunk
                deadline = time.time() + 0.3
        return total

    def discover(self) -> bytes | None:
        """Broadcast CMD_GET_SLAVE_INFO to find an unaddressed box. Returns
        the raw response (containing its UID) or None if nothing replied."""
        resp = self.send(BROADCAST_ALL_BOXES, 0x00, FN["CMD_GET_SLAVE_INFO"],
                          bytes([BROADCAST_ALL_BOXES, BROADCAST_ALL_BOXES]))
        return resp if resp else None

    def assign_address(self, uid: bytes, new_addr: int) -> bytes:
        """Assign `new_addr` to the box identified by `uid` (as extracted
        from discover()'s response)."""
        return self.send(BROADCAST_ALL_BOXES, 0x00, FN["CMD_SET_SLAVE_ADDR"],
                          bytes([new_addr]) + uid)

    def get_box_state(self, addr: int) -> bytes:
        return self.send(addr, 0xFF, FN["GET_BOX_STATE"])

    def get_filament_sensor_bitmask(self, addr: int, bank: int = 0x00) -> int | None:
        """bank=0x00 (MATERIAL): global 4-bit bitmask of which slots have
        filament present (bit0=A, bit1=B, bit2=C, bit3=D).
        bank=0x01 (CONNECTIONS): which slot(s) are currently mechanically
        "connected" to the shared feed path (see CTRL_CONNECTION_MOTOR_ACTION)."""
        resp = self.send(addr, 0xFF, FN["GET_FILAMENT_SENSOR_STATE"], bytes([bank]))
        if len(resp) >= 6:
            return resp[5]
        return None

    def set_box_mode_idle(self, addr: int) -> bytes:
        return self.send(addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))

    def ctrl_connection_motor(self, addr: int, action: int) -> bytes:
        """action: 0x00=STOP, 0x01=EXTRUDE (connect feed path), 0x02=RETRUDE.
        This is the step that was missing from early attempts at
        EXTRUDE_PROCESS — see docs/PROTOCOL.md."""
        return self.send(addr, 0xFF, FN["CTRL_CONNECTION_MOTOR_ACTION"], bytes([action]))

    def tighten_up(self, addr: int, enable: bool) -> bytes:
        return self.send(addr, 0xFF, FN["TIGHTEN_UP_ENABLE"], bytes([0x01 if enable else 0x00]))

    def extrude_stage(self, addr: int, slot: int, stage: int, amount: int = 0x00) -> bytes:
        return self.send(addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, stage, amount]))

    def retrude_stage(self, addr: int, slot: int, stage: int) -> bytes:
        return self.send(addr, 0xFF, FN["RETRUDE_PROCESS"], bytes([slot, stage]))
