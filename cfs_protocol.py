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

import struct
import time
import serial
import serial.tools.list_ports


DEFAULT_BAUD = 230400

# CFS boxes we've seen connect via a CH340/CH341 USB-serial adapter,
# vendor ID 0x1A86 (product ID 0x7523 for the specific one we tested with,
# but other CH34x variants exist under the same vendor).
CH340_VENDOR_ID = 0x1A86


def find_cfs_port(fallback: str = "/dev/ttyUSB0") -> str:
    """Best-effort auto-detect of the CFS box's serial port by matching
    the CH340/CH341 USB vendor ID, cross-platform (Windows COM ports,
    Linux /dev/ttyUSB*, etc. via pyserial's list_ports). Falls back to
    `fallback` if nothing matching is found or list_ports isn't supported
    on this platform - always double check this picked the right device
    if you have other CH340-based peripherals plugged in too."""
    try:
        for port in serial.tools.list_ports.comports():
            if port.vid == CH340_VENDOR_ID:
                return port.device
    except Exception:
        pass
    return fallback

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

# "Tip-forming" toolhead move sequence for a clean, non-jamming unload -
# found by examining Creality's own official firmware for this board
# variant (see FINDINGS.md in the private research log for provenance;
# this table and the orchestration around it are our own independent
# re-implementation of the *technique*, not a copy of anyone's code).
#
# The idea, same one used by e.g. Bambu's AMS and Prusa's MMU: before
# doing a long retraction, wiggle the extruder a small net distance
# first (alternating push/pull) so the filament tip re-melts and
# re-solidifies into a smooth taper instead of whatever shape it was
# left in - a blobby/snagged tip is what gets stuck in the extruder's
# drive gear on the way out. Plain "just retract" (what this repo did
# for a while) can jam on exactly that.
#
# Each entry is (distance_mm, speed_mm_per_min). Positive = extrude,
# negative = retract. The first 6 entries net only about -0.5mm of real
# movement - that's the wiggle. The last entry of the wiggle is
# deliberately very slow (60 mm/min) to give the tip time to actually
# solidify into shape before the real pull starts. The remaining 6
# entries are the real retraction - -15mm each, -90mm total, at
# increasing speed - clearing the tip fully out of the hotend/extruder
# gear area. In the real sequence each of these -15mm steps is preceded
# by a box-side RETRUDE_PROCESS call and can stop early once the
# toolhead sensor clears - see retrude_with_tip_form() in cfs_cli.py for
# that orchestration (it needs G-code + sensor access this pure protocol
# module doesn't have).
TIP_FORM_STEPS = [
    (0.5, 600), (-5, 600), (2.5, 600), (-1.25, 600), (1.75, 600), (1, 60),
    (-15, 90), (-15, 90), (-15, 500), (-15, 500), (-15, 500), (-15, 500),
]


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


def decode_measuring_wheel(data: bytes) -> float | None:
    """Decode a 4-byte measuring-wheel/odometer reading as the big-endian
    IEEE-754 float format documented by gitstonelabs/creality-cfs-klipper
    (credit: their reverse engineering, not ours) and confirmed against
    our own captured EXTRUDE_PROCESS stage-5 telemetry - see the worked
    example in docs/PROTOCOL.md. Values are negative and grow in magnitude
    while material is actively feeding, then flatten out once movement
    stops (e.g. filament reaching the toolhead sensor).

    Returns None if `data` isn't exactly 4 bytes (nothing to decode)."""
    if len(data) != 4:
        return None
    return struct.unpack(">f", data)[0]


class CFSClient:
    """Thin synchronous client. Not reactor-safe — do not use this directly
    inside a Klipper extra; see docs/PROTOCOL.md for notes on that."""

    def __init__(self, port: str | None = None, baud: int = DEFAULT_BAUD):
        """`port=None` (the default) auto-detects a CH340-family adapter
        via find_cfs_port(); pass an explicit path/COM port to skip
        auto-detection."""
        if port is None:
            port = find_cfs_port()
        self.port = port
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

    def get_version_sn(self, addr: int) -> str | None:
        """Returns the box's version/serial string, e.g. "113100008730633225CMPN"."""
        resp = self.send(addr, 0xFF, FN["GET_VERSION_SN"])
        if len(resp) >= 6:
            data = resp[5:-1]
            try:
                return data.decode("ascii")
            except UnicodeDecodeError:
                return None
        return None

    def get_rfid(self, addr: int, slot_index: int) -> str | None:
        """Returns RFID text, e.g. "A:none;" when no chip is present (most
        non-Creality-branded spools have no chip - expected, not an error).

        NOTE: `slot_index` here is a plain 0-3 index, NOT the bit0=A..bit3=D
        bitmask used by get_filament_sensor_bitmask()/EXTRUDE_PROCESS/etc.
        We tested indices 0-3 live and got back inconsistent-looking
        results (index 0 -> invalid, 1 -> "A:...", 2 -> "B:...", 3 ->
        "A:...;B:...;" both at once) that we never fully explained - treat
        this method's indexing as unresolved/exploratory, not something to
        build real slot-selection logic on top of yet."""
        resp = self.send(addr, 0xFF, FN["GET_RFID"], bytes([slot_index]))
        if len(resp) >= 6:
            data = resp[5:-1]
            try:
                return data.decode("ascii")
            except UnicodeDecodeError:
                return None
        return None

    def get_remain_len(self, addr: int, slot_index: int) -> bytes | None:
        """Returns the raw 4-byte remaining-length payload. Same plain 0-3
        `slot_index` convention as get_rfid() (see that docstring) - not
        the A/B/C/D bitmask used elsewhere. We haven't fully decoded this
        field's units/encoding either, so this returns raw bytes rather
        than a guessed numeric value - see docs/PROTOCOL.md."""
        resp = self.send(addr, 0xFF, FN["GET_REMAIN_LEN"], bytes([slot_index]))
        if len(resp) >= 9:
            return resp[5:9]
        return None

    def get_filament_sensor_bitmask(self, addr: int, bank: int = 0x00) -> int | None:
        """bank=0x00 (MATERIAL): global 4-bit bitmask of which slots have
        filament present (bit0=A, bit1=B, bit2=C, bit3=D).
        bank=0x01 (CONNECTIONS): which slot(s) are currently mechanically
        "connected" to the shared feed path (see CTRL_CONNECTION_MOTOR_ACTION)."""
        resp = self.send(addr, 0xFF, FN["GET_FILAMENT_SENSOR_STATE"], bytes([bank]))
        if len(resp) >= 6:
            return resp[5]
        return None

    def set_box_mode(self, addr: int, mode: str, slot: int = 0x00) -> bytes:
        """mode: "PRINT" or "IDLE". slot: a single SLOT_A..SLOT_D bitmask,
        or 0x00 (the default) for "no slot" - the generic form used to
        bracket a sequence. Real wire payload is [slot, mode_byte]
        (mode_byte: PRINT=0x00, IDLE=0x01) - confirmed by decompiling the
        real official firmware (see FINDINGS.md in the private research
        log; not reproduced here, just the byte layout it revealed).
        set_box_mode_idle() covers the generic no-slot case; use this
        directly for the per-slot PRINT-mode form the official sequence
        sends once a specific slot has finished loading."""
        mode_byte = {"PRINT": 0x00, "IDLE": 0x01}[mode]
        return self.send(addr, 0xFF, FN["SET_BOX_MODE"], bytes([slot, mode_byte]))

    def set_box_mode_idle(self, addr: int) -> bytes:
        """Generic "enter feed mode" IDLE, no specific slot (slot=0x00).
        See set_box_mode() for the per-slot PRINT-mode form."""
        return self.set_box_mode(addr, "IDLE", slot=0x00)

    def ctrl_connection_motor(self, addr: int, action: int) -> bytes:
        """action: 0x00=STOP, 0x01=EXTRUDE (connect feed path), 0x02=RETRUDE.
        This is the step that was missing from early attempts at
        EXTRUDE_PROCESS — see docs/PROTOCOL.md."""
        return self.send(addr, 0xFF, FN["CTRL_CONNECTION_MOTOR_ACTION"], bytes([action]))

    def tighten_up(self, addr: int, enable: bool) -> bytes:
        return self.send(addr, 0xFF, FN["TIGHTEN_UP_ENABLE"], bytes([0x01 if enable else 0x00]))

    def extrude_stage(self, addr: int, slot: int, stage: int, amount: int = 0x00) -> bytes:
        """Real wire payload, confirmed LIVE on our own hardware: 3 bytes,
        [slot, stage, amount]. We briefly changed this to a 2-byte
        [slot, stage] form (matching what decompiling Creality's official
        firmware's *host-side* driver code appeared to send - see
        FINDINGS.md) but that regressed live: even slot A, which had
        worked reliably for sessions, started failing PARAMS_ERR
        immediately at stage 0 with the 2-byte form, and went back to
        producing real (if partial/unclear) motor movement the moment we
        reverted to 3 bytes. Our best guess: this specific CFS unit's own
        onboard firmware (which is what we're actually talking to over
        RS-485) doesn't necessarily match whatever transport-layer framing
        the decompiled *printer-side* driver code assumes - trust live
        hardware behavior over source code when they disagree. stage=7's
        real amount byte is unconfirmed; passing 0x02 there (as the
        decompiled source's special-cased extra byte) is a reasonable
        guess, not yet verified live either way."""
        return self.send(addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, stage, amount]))

    def retrude_stage(self, addr: int, slot: int, stage: int) -> bytes:
        return self.send(addr, 0xFF, FN["RETRUDE_PROCESS"], bytes([slot, stage]))

    def get_buffer_state(self, addr: int) -> int | None:
        """Returns the buffer fill state: 0=middle, 1=full, 2=empty."""
        resp = self.send(addr, 0xFF, FN["GET_BUFFER_STATE"])
        if len(resp) >= 6:
            return resp[5]
        return None
