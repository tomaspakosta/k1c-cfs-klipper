# Creality CFS support for Klipper
#
# Wraps the protocol validated in cfs_protocol.py / docs/PROTOCOL.md into
# a real Klipper extra: gcode commands + a periodic status poll.
#
# STATUS: the underlying protocol (framing, CRC8, command bytes) is
# live-validated against real hardware - see the repo root README and
# docs/PROTOCOL.md. This *file* - the Klipper integration itself (config
# parsing, gcode command registration, reactor timer) - has NOT yet been
# loaded into a running Klipper and tested. Review it, install it
# carefully, and test each command individually before relying on it.
#
# KNOWN LIMITATION: this uses blocking pyserial calls from gcode command
# handlers. Klipper's reactor is single-threaded, so a slow/stalled CFS
# response can briefly stall the whole reactor (MCU keepalive, other
# gcode processing) for up to the timeout on that call. For a small
# number of manually-triggered commands this is usually fine in practice,
# but it's not how a "proper" Klipper extra should be built long-term -
# see gitstonelabs/creality-cfs-klipper's reactor.register_fd()-based
# non-blocking approach (referenced in this repo's README credits) for
# how to do this right. Fixing this is on the roadmap, not done yet.
#
# Installation: copy this file into klipper/klippy/extras/creality_cfs.py
# on the printer, add a [creality_cfs] section to printer.cfg (see example
# below), then restart Klipper.
#
# Example printer.cfg section:
#
#   [creality_cfs]
#   serial: /dev/ttyUSB0
#   baud: 230400
#   box_addr: 1
#   # optional, see the CFS_EXTRUDE "go to extrude position" note further
#   # down - defaults below are from a factory box.cfg on the same board
#   # variant, override if yours differ:
#   extrude_pos_x: 148.0
#   extrude_pos_y: 225.3
#   extrude_pos_z: 30.0
#   # optional, name of your real toolhead [filament_switch_sensor],
#   # used by CFS_RETRUDE's tip-form unload sequence:
#   toolhead_sensor_name: filament_sensor_2

import logging
import time

try:
    import serial
except ImportError:
    serial = None


FN = {
    "GET_RFID": 0x02,
    "GET_REMAIN_LEN": 0x03,
    "SET_BOX_MODE": 0x04,
    "GET_BUFFER_STATE": 0x05,
    "CTRL_CONNECTION_MOTOR_ACTION": 0x07,
    "GET_FILAMENT_SENSOR_STATE": 0x08,
    "GET_BOX_STATE": 0x0A,
    "TIGHTEN_UP_ENABLE": 0x0F,
    "EXTRUDE_PROCESS": 0x10,
    "RETRUDE_PROCESS": 0x11,
    "GET_VERSION_SN": 0x14,
    "CMD_SET_SLAVE_ADDR": 0xA0,
    "CMD_GET_SLAVE_INFO": 0xA1,
    "CMD_ONLINE_CHECK": 0xA2,
}

SLOT_BYTES = {"A": 0x01, "B": 0x02, "C": 0x04, "D": 0x08}
BROADCAST_ALL_BOXES = 0xFE

# "Tip-forming" toolhead move sequence for a clean, non-jamming unload -
# see the identical table (and its full rationale) in cfs_protocol.py's
# TIP_FORM_STEPS. Duplicated here rather than imported since this file
# is meant to be self-contained when copied into klippy/extras/.
TIP_FORM_STEPS = [
    (0.5, 600), (-5, 600), (2.5, 600), (-1.25, 600), (1.75, 600), (1, 60),
    (-15, 90), (-15, 90), (-15, 500), (-15, 500), (-15, 500), (-15, 500),
]


class CFSSlotSensor:
    """A minimal object matching the shape of Klipper's built-in
    filament_switch_sensor (filament_detected + enabled). Registered under
    the name "filament_switch_sensor CFS_<slot>" so Fluidd/Mainsail pick it
    up in their normal filament-sensor UI automatically - no custom
    frontend needed. See klipper_extra/README.md for what this looks like.
    """

    def __init__(self):
        self.filament_detected = False
        self.enabled = True

    def get_status(self, eventtime):
        return {"filament_detected": self.filament_detected, "enabled": self.enabled}


def crc8(data):
    crc = 0
    for byte in bytearray(data):
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07 if crc & 0x80 else crc << 1) & 0xFF
    return crc


def build_frame(slave_addr, status, function_code, data=b""):
    length = 1 + 1 + len(data) + 1
    body = bytes([length, status, function_code]) + bytes(data)
    crc = crc8(body)
    return bytes([0xF7, slave_addr]) + body + bytes([crc])


class CrealityCFS:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")

        if serial is None:
            raise config.error(
                "creality_cfs: pyserial is not importable in this Klipper "
                "environment - install it in klippy-env before using this extra")

        self.serial_path = config.get("serial", "/dev/ttyUSB0")
        self.baud = config.getint("baud", 230400)
        self.box_addr = config.getint("box_addr", 1)
        self.poll_interval = config.getfloat("poll_interval", 5.0, above=0.0)

        # "Go to extrude position" before EXTRUDE_PROCESS - a step our own
        # testing NEVER did, taken from a real factory box.cfg found on the
        # same board variant (CR4CU220812S12) restored from /rom on a
        # different K1C. That printer's official box.py sequence is
        # BOX_ERROR_CLEAR -> ... -> BOX_GO_TO_EXTRUDE_POS -> BOX_NOZZLE_CLEAN
        # -> BOX_EXTRUDE_MATERIAL -> BOX_EXTRUDER_EXTRUDE -> ... and worked
        # on all 4 slots there (until a later, still-unexplained regression
        # to slot-A-only - see the project memory / FINDINGS.md). We have
        # NEVER tested this position move ourselves - defaults below are
        # copied verbatim from that factory box.cfg, override in printer.cfg
        # if your machine's coordinates differ.
        self.extrude_pos_x = config.getfloat("extrude_pos_x", 148.0)
        self.extrude_pos_y = config.getfloat("extrude_pos_y", 225.3)
        self.extrude_pos_z = config.getfloat("extrude_pos_z", 30.0)
        self.extrude_move_speed = config.getfloat("extrude_move_speed", 3600.0, above=0.0)

        # Name of the real toolhead filament sensor (a plain Klipper
        # [filament_switch_sensor], NOT one of this extra's own virtual
        # CFS_A..CFS_D sensors) - used by the tip-form unload sequence in
        # cmd_CFS_RETRUDE to know when it's actually safe to stop.
        self.toolhead_sensor_name = config.get("toolhead_sensor_name", "filament_sensor_2")

        self.ser = None
        self.addressed = False
        self.last_status = {}

        # Register one virtual filament_switch_sensor per slot so Fluidd/
        # Mainsail show CFS material presence in their normal filament
        # sensor panel, without any custom frontend work. Sensor names are
        # "filament_switch_sensor CFS_A".."CFS_D".
        name_prefix = config.get("sensor_name_prefix", "CFS_")
        self.slot_sensors = {}
        for slot_letter in SLOT_BYTES:
            sensor = CFSSlotSensor()
            self.printer.add_object(
                "filament_switch_sensor %s%s" % (name_prefix, slot_letter), sensor)
            self.slot_sensors[slot_letter] = sensor

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

        gcode = self.gcode
        gcode.register_command("CFS_STATUS", self.cmd_CFS_STATUS,
                                desc="Report CFS box status and sensor state")
        gcode.register_command("CFS_RETRUDE", self.cmd_CFS_RETRUDE,
                                desc="CFS_RETRUDE SLOT=<A|B|C|D> - reel filament back onto the spool")
        gcode.register_command("CFS_EXTRUDE", self.cmd_CFS_EXTRUDE,
                                desc="CFS_EXTRUDE SLOT=<A|B|C|D> [POLLS=<n>] - feed filament from the spool")

    # -- low level transport -------------------------------------------

    def _open(self):
        if self.ser is None:
            self.ser = serial.Serial(self.serial_path, baudrate=self.baud, timeout=1.0)

    def _send(self, slave_addr, status, function_code, data=b"", timeout=2.0):
        self._open()
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

    # -- lifecycle --------------------------------------------------------

    def _handle_connect(self):
        try:
            self._discover_and_address()
        except Exception:
            logging.exception("creality_cfs: discovery/addressing failed at klippy:connect")
            return
        self.reactor.register_timer(self._poll_timer, self.reactor.NOW)

    def _handle_disconnect(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def _discover_and_address(self):
        resp = self._send(BROADCAST_ALL_BOXES, 0x00, FN["CMD_GET_SLAVE_INFO"],
                           bytes([BROADCAST_ALL_BOXES, BROADCAST_ALL_BOXES]))
        if len(resp) < 20:
            logging.warning("creality_cfs: no CFS box responded to discovery")
            return
        uid = resp[7:19]
        self._send(BROADCAST_ALL_BOXES, 0x00, FN["CMD_SET_SLAVE_ADDR"],
                    bytes([self.box_addr]) + uid)
        self.addressed = True
        logging.info("creality_cfs: addressed box at %#04x, uid=%s",
                     self.box_addr, uid.hex())

    def _poll_timer(self, eventtime):
        if self.addressed:
            try:
                resp = self._send(self.box_addr, 0xFF, FN["GET_BOX_STATE"])
                self.last_status["box_state"] = resp.hex() if resp else None
                sensor = self._send(self.box_addr, 0xFF, FN["GET_FILAMENT_SENSOR_STATE"], bytes([0x00]))
                if len(sensor) >= 6:
                    bitmask = sensor[5]
                    self.last_status["material_bitmask"] = bitmask
                    for slot_letter, slot_byte in SLOT_BYTES.items():
                        self.slot_sensors[slot_letter].filament_detected = bool(bitmask & slot_byte)
            except Exception:
                logging.exception("creality_cfs: poll failed")
        return eventtime + self.poll_interval

    # -- gcode commands -----------------------------------------------

    def cmd_CFS_STATUS(self, gcmd):
        if not self.addressed:
            gcmd.respond_info("CFS box not addressed (no response at klippy:connect)")
            return
        bitmask = self.last_status.get("material_bitmask")
        if bitmask is not None:
            loaded = [name for name, bit in SLOT_BYTES.items() if bitmask & bit]
            gcmd.respond_info("CFS: material loaded in slots: %s" % (", ".join(loaded) or "none"))
        else:
            gcmd.respond_info("CFS: no status polled yet")

    def _toolhead_filament_detected(self):
        sensor = self.printer.lookup_object(
            "filament_switch_sensor %s" % self.toolhead_sensor_name, None)
        if sensor is None:
            return None
        return sensor.get_status(self.reactor.monotonic())["filament_detected"]

    def _retrude_with_tip_form(self, gcmd):
        # UNTESTED (as of this writing) reimplementation of the real
        # official firmware's unload sequence - see TIP_FORM_STEPS above
        # and FINDINGS.md in the private research log for where this came
        # from and why: a single box-side RETRUDE_PROCESS call (what this
        # repo did before) can leave filament jammed in the toolhead
        # extruder's own drive gear, needing a manual lever release -
        # confirmed live, see docs/TOOLCHANGE_TEST_PLAN.md.
        #
        # First "wiggles" the extruder a small net distance to re-melt and
        # re-shape the filament tip into a smooth taper - a blobby/snagged
        # tip is what catches in the gear on the way out - then does the
        # real retraction in -15mm chunks, checking in with the box (a
        # generic, no-specific-slot RETRUDE_PROCESS call) and the toolhead
        # sensor between chunks so it can stop as soon as filament is
        # confirmed clear.
        self.gcode.run_script_from_command("M83")
        for dist, speed in TIP_FORM_STEPS:
            if dist <= -10:
                resp = self._send(self.box_addr, 0xFF, FN["RETRUDE_PROCESS"], bytes([0x00, 0x00]))
                status = resp[3] if len(resp) >= 4 else None
                if status is None:
                    gcmd.respond_info("CFS_RETRUDE: no reply from box mid-unload, "
                                       "stopping - check physically")
                    return False
                if status != 0x00:
                    self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))
                    detected = self._toolhead_filament_detected()
                    if detected is False:
                        gcmd.respond_info("CFS_RETRUDE: toolhead sensor clear, "
                                           "unload complete (stopped early)")
                        return True
                    # Sensor still sees filament (or no sensor configured) -
                    # keep going with the remaining steps.
            self.gcode.run_script_from_command("G0 E%.2f F%.0f" % (dist, speed))
            self.gcode.run_script_from_command("M400")
        detected = self._toolhead_filament_detected()
        return detected is False

    def cmd_CFS_RETRUDE(self, gcmd):
        slot_letter = gcmd.get("SLOT", "A").upper()
        if slot_letter not in SLOT_BYTES:
            raise gcmd.error("SLOT must be one of A, B, C, D")
        slot = SLOT_BYTES[slot_letter]

        self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))
        time.sleep(0.3)
        self._send(self.box_addr, 0xFF, FN["RETRUDE_PROCESS"], bytes([slot, 0x00]))
        time.sleep(0.5)
        self._send(self.box_addr, 0xFF, FN["RETRUDE_PROCESS"], bytes([slot, 0x01]))

        ok = self._retrude_with_tip_form(gcmd)
        gcmd.respond_info("CFS_RETRUDE slot=%s complete (tip-form unload %s)" % (
            slot_letter, "confirmed clear" if ok else "did NOT confirm clear - check physically"))

    def cmd_CFS_EXTRUDE(self, gcmd):
        # UNTESTED CHANGE (not yet run live - see FINDINGS.md in the private
        # research log for the full story): after exhausting live guessing
        # (7 failed attempts at slots other than A, always the same
        # EXTRUDE_ERR8/FR2832 failure), we downloaded Creality's real
        # official K1C firmware for this board variant and decompiled its
        # actual box.py modules - see FINDINGS.md's "PRŮLOM" section. That
        # gave us ground truth instead of guesses:
        #   1. An error-clear-equivalent BEFORE starting, not just cleanup
        #      after (best effort - we log GET_BOX_STATE, then SET_BOX_MODE
        #      IDLE; we don't know exactly what stock BOX_ERROR_CLEAR puts
        #      on the wire since box_wrapper.py didn't decompile cleanly).
        #   2. BOX_GO_TO_EXTRUDE_POS - move the toolhead to a specific
        #      position before EXTRUDE_PROCESS (coords from a real factory
        #      box.cfg, see __init__ / printer.cfg to override).
        #   3. THE key missing piece: the real sequence does NOT stop after
        #      polling stage 5 until the toolhead sensor trips, like we
        #      always did. It continues: M83 + a slow toolhead-side G0 E10
        #      F35 move, THEN EXTRUDE_PROCESS stage 6, THEN another M83 + an
        #      even slower G0 E5 F10, THEN stage 7 - and only then marks the
        #      slot as loaded via the per-slot PRINT form of SET_BOX_MODE
        #      (payload [slot_bitmask, 0x00], confirmed from the real
        #      firmware - our old belief that SET_BOX_MODE always took a
        #      fixed [0x00, mode] pair was wrong, see set_box_mode() below).
        #      We had NEVER done any of this - we always stopped right
        #      after stage 5 and went straight to cleanup. This may be why
        #      the box never properly "finished" a load internally, which
        #      would explain both the post-run latched-error gotcha above
        #      AND why it could never switch to a different slot afterwards.
        #   4. EXTRUDE_PROCESS's real payload is [slot, stage] (2 bytes),
        #      not the 3 bytes ([slot, stage, 0x00]) we always sent - real
        #      firmware only appends an extra byte (fixed 0x02) for stage 7
        #      specifically. Fixed below; probably harmless before, but not
        #      what the real protocol does.
        # Still not implemented: BOX_NOZZLE_CLEAN (a wipe step the stock
        # sequence also runs) and the cutter-homing RS485 exchange the real
        # firmware does via its own cut_action object before every load
        # (we currently home the cutter with plain G-code moves instead,
        # which is physically confirmed to work, just not how stock does it).
        slot_letter = gcmd.get("SLOT", "A").upper()
        if slot_letter not in SLOT_BYTES:
            raise gcmd.error("SLOT must be one of A, B, C, D")
        slot = SLOT_BYTES[slot_letter]
        polls = gcmd.get_int("POLLS", 20, minval=1, maxval=200)

        toolhead = self.printer.lookup_object("toolhead")
        homed = toolhead.get_status(self.reactor.monotonic())["homed_axes"]
        if not all(axis in homed for axis in "xyz"):
            raise gcmd.error("CFS_EXTRUDE: home the printer first (G28) - "
                              "refusing to move to the extrude position unhomed")

        # Step 1: error-clear-equivalent (see note above - best effort only)
        status = self._send(self.box_addr, 0xFF, FN["GET_BOX_STATE"])
        if status:
            gcmd.respond_info("CFS_EXTRUDE: pre-run GET_BOX_STATE=%s" % status.hex())
        self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))
        time.sleep(0.3)

        # Step 2: BOX_GO_TO_EXTRUDE_POS equivalent - untested, see note above
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=CFS_EXTRUDE")
        self.gcode.run_script_from_command(
            "G90\nG1 X%.2f Y%.2f Z%.2f F%.0f" % (
                self.extrude_pos_x, self.extrude_pos_y, self.extrude_pos_z,
                self.extrude_move_speed))
        self.gcode.run_script_from_command("M400")

        self._send(self.box_addr, 0xFF, FN["CTRL_CONNECTION_MOTOR_ACTION"], bytes([0x01]))
        time.sleep(0.5)
        self._send(self.box_addr, 0xFF, FN["TIGHTEN_UP_ENABLE"], bytes([0x01]))
        time.sleep(0.3)

        # EXTRUDE_PROCESS payload is [slot, stage, amount] - 3 bytes,
        # amount usually 0x00. We briefly tried a 2-byte [slot, stage] form
        # (matching what decompiling Creality's official *host-side*
        # driver code appeared to send - see FINDINGS.md) but that
        # regressed live: even slot A, reliable for many sessions, started
        # failing PARAMS_ERR immediately with 2 bytes, and went back to
        # producing real motor movement the moment we reverted to 3.
        # Trust live hardware behavior over decompiled source when they
        # disagree - this box's own onboard firmware apparently doesn't
        # match whatever transport framing the host-side code assumes.
        self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x00, 0x00]))
        time.sleep(0.3)
        self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x04, 0x00]))
        time.sleep(0.3)

        for _ in range(polls):
            self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x05, 0x00]))
            time.sleep(0.4)

        # Stages 6/7 + the toolhead-side prime moves between them - see the
        # header comment above cmd_CFS_EXTRUDE. UNTESTED.
        self.gcode.run_script_from_command("M83")
        self.gcode.run_script_from_command("G0 E10 F35")
        time.sleep(0.3)
        self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x06, 0x00]))
        time.sleep(0.3)
        self.gcode.run_script_from_command("M83")
        self.gcode.run_script_from_command("G0 E5 F10")
        time.sleep(0.3)
        # stage 7's real 3rd byte is unconfirmed live - 0x02 is the
        # decompiled source's special-cased extra byte, used here as a
        # reasonable guess, not yet verified either way.
        self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x07, 0x02]))
        time.sleep(0.3)
        # Mark this specific slot as the active PRINT-mode slot - payload
        # [slot_bitmask, 0x00], NOT the fixed [0x00, mode] pair used for the
        # generic enter-feed-mode / cleanup calls elsewhere in this file.
        self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([slot, 0x00]))

        self._send(self.box_addr, 0xFF, FN["TIGHTEN_UP_ENABLE"], bytes([0x00]))
        self._send(self.box_addr, 0xFF, FN["CTRL_CONNECTION_MOTOR_ACTION"], bytes([0x00]))
        # A completed run can leave the box reporting a latched error status
        # (seen live: EXTRUDE_ERR8 on GET_BOX_STATE) even when the extrude
        # itself succeeded (toolhead sensor confirmed). A fresh
        # SET_BOX_MODE(IDLE) clears it - confirmed live on real hardware.
        self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))

        # RESTORE_POSITION equivalent
        self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=CFS_EXTRUDE")
        gcmd.respond_info("CFS_EXTRUDE slot=%s complete (%d polls)" % (slot_letter, polls))


def load_config(config):
    return CrealityCFS(config)
