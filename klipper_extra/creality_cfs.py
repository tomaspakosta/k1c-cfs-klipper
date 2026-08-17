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
# KNOWN LIMITATION: this uses blocking pyserial calls (_send()'s own
# ser.read() loop) from gcode command handlers. Klipper's reactor is
# single-threaded, so a slow/stalled CFS response can briefly stall the
# whole reactor (MCU keepalive, other gcode processing, and critically
# its own heater PID/watchdog timers) for up to the timeout on that call.
# This is not theoretical: live 2026-08-16, a CFS_EXTRUDE run's
# accumulated stall time tripped a false verify_heater "not heating at
# expected rate" shutdown mid-run (see FINDINGS.md). The plain
# time.sleep() calls that made up part of that stall are fixed - they now
# use self._pause() (reactor.pause(), cooperative) instead - but the
# repeated blocking ser.read() calls inside _send() itself, especially the
# ~20x poll loop in cmd_CFS_EXTRUDE, remain a real, NOT-yet-fixed source
# of the same class of stall. For a small number of manually-triggered
# commands this is usually fine in practice, but it's not how a "proper"
# Klipper extra should be built long-term - see
# gitstonelabs/creality-cfs-klipper's reactor.register_fd()-based
# non-blocking approach (referenced in this repo's README credits) for
# how to do this right. Fixing the serial I/O itself is on the roadmap,
# not done yet - if you hit another heater_fault/reactor-stall-shaped
# problem, this is where to look next.
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
#   # REQUIRED before CFS_EXTRUDE will run at all - no default is shipped
#   # any more (see the safety incident note by extrude_pos_x/y/z further
#   # down). Calibrate these live on YOUR printer first: home, then jog
#   # there in small steps from the console, watching closely the whole
#   # time, well clear of anything (cameras, frame, wiring) before you
#   # trust CFS_EXTRUDE to drive there on its own:
#   extrude_pos_x: <calibrate this - see docs/MANUAL.md>
#   extrude_pos_y: <calibrate this - see docs/MANUAL.md>
#   extrude_pos_z: <calibrate this - see docs/MANUAL.md>
#   # optional, name of your real toolhead [filament_switch_sensor],
#   # used by CFS_RETRUDE's tip-form unload sequence:
#   toolhead_sensor_name: filament_sensor_2

import logging
import struct
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
    "SET_PRE_LOADING": 0x0D,
    "GET_MEASURING_WHEEL": 0x0E,
    "TIGHTEN_UP_ENABLE": 0x0F,
    "EXTRUDE_PROCESS": 0x10,
    "RETRUDE_PROCESS": 0x11,
    "GET_VERSION_SN": 0x14,
    "MOVE_DISTANCE": 0x31,
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


def decode_measuring_wheel(data):
    """Decode a 4-byte measuring-wheel/odometer reading - big-endian
    IEEE-754 float, mm, negative and growing in magnitude while material
    actively feeds. Duplicated from cfs_protocol.py (same reasoning as
    TIP_FORM_STEPS - self-contained file). Confirmed correct against our
    own real EXTRUDE_PROCESS telemetry, and independently re-derived
    2026-08-17 from decompiled reference firmware's own
    get_measuring_wheel() - its convoluted big-endian-int-then-repack-
    little-endian-float dance is mathematically identical to a plain
    big-endian float unpack, see FINDINGS.md."""
    if len(data) != 4:
        return None
    return struct.unpack(">f", data)[0]


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

        # "Go to extrude position" before EXTRUDE_PROCESS.
        #
        # SAFETY INCIDENT 2026-08-16: this used to default to X148/Y225.3/
        # Z30, copied verbatim (never physically tested by us) from a
        # factory box.cfg found on a different K1C. Live on THIS printer,
        # that move crashed the toolhead into the frame/enclosure near
        # where an overhead camera is mounted - the user had to hit
        # emergency stop. No injury/damage beyond a startled camera mount,
        # but this is a real collision hazard, not a theoretical one.
        # There is now NO default - you must explicitly set all three of
        # extrude_pos_x/y/z in printer.cfg yourself, calibrated live on
        # YOUR printer the same careful way the cut and purge positions
        # were calibrated (small G1 jogs from the console, watching the
        # whole time, well before trusting a macro to do it automatically -
        # see docs/MANUAL.md). cmd_CFS_EXTRUDE refuses to run at all until
        # these are set. The move itself is also now split into separate
        # Z-then-XY-then-Z legs (see below) instead of one diagonal G1, so
        # a wrong number is less likely to carve a shortcut through
        # something solid - but that is not a substitute for calibrating
        # real numbers for your machine.
        self.extrude_pos_x = config.getfloat("extrude_pos_x", None)
        self.extrude_pos_y = config.getfloat("extrude_pos_y", None)
        self.extrude_pos_z = config.getfloat("extrude_pos_z", None)
        # Slower default than before (was 3600) - a wrong/uncalibrated
        # position is easier to e-stop in time at a lower speed.
        self.extrude_move_speed = config.getfloat("extrude_move_speed", 1500.0, above=0.0)

        # EXTRUDE stage 5->6->7 handoff tuning - see _extrude_material_handoff()
        # below for the full sequence this drives. UPDATED 2026-08-17: an
        # earlier version of this file just raised the toolhead priming
        # distances (prime_e1/e2, 10mm/5mm -> 20mm/15mm) after live testing
        # showed filament reaching the toolhead sensor but the extruder gear
        # never actually grabbing it - that was a reasonable guess at the
        # time, but decompiling the real firmware's extrude_material()
        # function (previously a decompyle3 parse failure, only readable via
        # raw bytecode disassembly - see FINDINGS.md) revealed the ACTUAL
        # missing piece is architectural, not distance: the real firmware
        # runs a bounded RETRY LOOP here, verified against the box's own
        # measuring-wheel/odometer reading (not just a toolhead sensor
        # check) after each attempt, using a short but FAST push
        # (default 9mm at F12000 - 200mm/s, not our old slow F35/F10) -
        # repeated up to extrude_material_times times, with a toolhead+box
        # retreat-and-retry recovery step if a given attempt's stage 7
        # doesn't come back OK. prime_e1/prime_e2 (the fixed 10mm/5mm moves
        # immediately around stage 6 and inside the retry loop's stage 7
        # branch) turned out to be correct in the real firmware after all -
        # reverted to their original values here now that the real
        # bottleneck is understood to be architecture, not raw distance.
        self.prime_e1 = config.getfloat("prime_e1", 10.0)
        self.prime_e2 = config.getfloat("prime_e2", 5.0)
        self.extrude_material_len_for_extruder = config.getfloat(
            "extrude_material_len_for_extruder", 9.0, minval=0.0, maxval=60.0)
        self.extrude_material_times = config.getint("extrude_material_times", 6, minval=1)

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
        gcode.register_command("CFS_SET_PRE_LOADING", self.cmd_CFS_SET_PRE_LOADING,
                                desc="CFS_SET_PRE_LOADING ACTION=<CLOSE|OPEN|RUN|TIGHT> [MASK=<0-15>] "
                                     "- mostly for diagnostics; CLOSE is run automatically as a reset "
                                     "step at the start of CFS_RETRUDE/CFS_EXTRUDE. RUN/TIGHT are slow "
                                     "(~38s/slot) and untested by us - use supervised.")
        gcode.register_command("CFS_RECONNECT", self.cmd_CFS_RECONNECT,
                                desc="CFS_RECONNECT - retry discovery/addressing manually. The "
                                     "automatic attempt at klippy:connect is a single try with no "
                                     "retry (see KNOWN LIMITATION at the top of this file) and can "
                                     "lose a race with the USB device settling - if CFS_STATUS says "
                                     "'not addressed' after startup even though the box is known "
                                     "good, run this instead of a full restart.")
        gcode.register_command("CFS_SYNC_FEED", self.cmd_CFS_SYNC_FEED,
                                desc="CFS_SYNC_FEED [DIST=<mm, default 100>] - DIAGNOSTIC, not "
                                     "part of normal use. Fires a raw box feed-motor move and a "
                                     "toolhead G1 E move of the same distance as close to "
                                     "simultaneously as possible, to test whether genuinely "
                                     "synchronized box+toolhead feeding pushes filament through "
                                     "where the staged EXTRUDE_PROCESS sequence hasn't. Requires "
                                     "a hot nozzle and homed axes are not needed.")

    # -- low level transport -------------------------------------------

    def _open(self):
        if self.ser is None:
            self.ser = serial.Serial(self.serial_path, baudrate=self.baud, timeout=1.0)

    def _pause(self, seconds):
        """Cooperative wait - use instead of a raw time.sleep() anywhere in
        this file. A plain time.sleep() hard-blocks Klipper's whole
        single-threaded reactor, including its own heater PID/watchdog
        timers; reactor.pause() yields to the reactor while waiting, so
        those keep running normally. Found live 2026-08-16 (see
        FINDINGS.md): a CFS_EXTRUDE run's accumulated time.sleep() calls
        stalled the reactor long enough that verify_heater's watchdog
        missed its update window and tripped a false "Heater extruder not
        heating at expected rate" shutdown, even though the hotend itself
        was fine. This fixes the sleep-based part of that; the blocking
        pyserial reads inside _send() are a separate, deeper source of the
        same class of stall that this does NOT fix - see the file header's
        KNOWN LIMITATION and _send()'s own comment."""
        self.reactor.pause(self.reactor.monotonic() + seconds)

    def _send(self, slave_addr, status, function_code, data=b"", timeout=2.0, debug=False):
        self._open()
        frame = build_frame(slave_addr, status, function_code, data)
        self.ser.reset_input_buffer()
        if debug:
            logging.info("creality_cfs: TX %s", frame.hex())
        self.ser.write(frame)
        self.ser.flush()
        deadline = time.time() + timeout
        total = b""
        while time.time() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                total += chunk
                deadline = time.time() + 0.3
        if debug:
            logging.info("creality_cfs: RX %s", total.hex() if total else "(nothing)")
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

    def _discover_and_address(self, attempts=3, retry_pause=1.5):
        # ROOT CAUSE FOUND 2026-08-16 (see FINDINGS.md for the full
        # diagnostic trail): this box remembers its RS-485 address across
        # power cycles. Once it has been addressed once, it simply stops
        # replying to broadcast discovery (CMD_GET_SLAVE_INFO at the
        # broadcast address) - it's not listening there any more, not a
        # timing/reactor/environment problem. It answers a direct query at
        # its already-known address instantly and reliably. This is
        # exactly why cfs_cli.py's `status` command always worked: it
        # never does discovery either, it just talks straight to
        # box_addr=1. This extra's old code insisted on broadcast-first
        # with no fallback, so it failed every time on an
        # already-addressed box - confirmed identically whether run
        # inside Klipper, under klippy-env's own Python standalone, or
        # under the system Python: the execution context was never the
        # actual variable.
        #
        # Fix: try a cheap direct probe at box_addr first. Only fall back
        # to full broadcast discovery (for a genuinely fresh/unaddressed
        # box - e.g. first-ever run, or after replacing the box) if that
        # direct probe gets no reply.
        resp = self._send(self.box_addr, 0xFF, FN["CMD_ONLINE_CHECK"])
        if resp:
            self.addressed = True
            logging.info("creality_cfs: box already addressed at %#04x, "
                         "skipped broadcast discovery (direct probe replied)",
                         self.box_addr)
            return
        logging.info("creality_cfs: no reply from a direct probe at %#04x, "
                     "falling back to broadcast discovery (box may be "
                     "genuinely unaddressed)", self.box_addr)
        for attempt in range(1, attempts + 1):
            # debug=True here logs the raw TX/RX bytes at INFO level (always
            # visible in klippy.log, unlike logging.debug which Klipper's
            # default log setup may filter out) - added specifically to
            # diagnose the still-open addressing bug documented above: is
            # the write actually reaching the box, or is the read side
            # never getting a reply that's really there? Next physical
            # session, trigger CFS_RECONNECT (or a restart) and grep
            # klippy.log for "creality_cfs: TX"/"creality_cfs: RX" to see
            # exactly what went out and what (if anything) came back.
            resp = self._send(BROADCAST_ALL_BOXES, 0x00, FN["CMD_GET_SLAVE_INFO"],
                               bytes([BROADCAST_ALL_BOXES, BROADCAST_ALL_BOXES]),
                               debug=True)
            if len(resp) >= 20:
                uid = resp[7:19]
                self._send(BROADCAST_ALL_BOXES, 0x00, FN["CMD_SET_SLAVE_ADDR"],
                            bytes([self.box_addr]) + uid)
                self.addressed = True
                logging.info("creality_cfs: addressed box at %#04x, uid=%s "
                             "(attempt %d/%d)", self.box_addr, uid.hex(),
                             attempt, attempts)
                return
            logging.warning("creality_cfs: no CFS box responded to discovery "
                            "(attempt %d/%d)", attempt, attempts)
            if attempt < attempts:
                if self.ser is not None:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                self._pause(retry_pause)

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

    def _reset_pre_loading(self):
        """CLOSE (disable) pre-loading on all 4 slots - a cheap, fast,
        non-motor call the real official sequence sends as a "reset to
        known state" step before every toolchange (see FINDINGS.md in
        the private research log). Best-effort - failures here shouldn't
        block the actual retrude/extrude that follows."""
        try:
            self._send(self.box_addr, 0xFF, FN["SET_PRE_LOADING"], bytes([0x0F, 0x00]))
        except Exception:
            logging.exception("creality_cfs: pre-loading reset failed (non-fatal)")

    def _get_measuring_wheel(self):
        """Read the box's measuring-wheel/odometer distance (fn 0x0E,
        data=[0x01] = the real firmware's "GET" action byte - confirmed
        2026-08-17 from decompiled reference, see FINDINGS.md). Returns
        None if the box didn't reply with a valid 4-byte reading - callers
        must handle that (treat as "can't verify", not "definitely 0")."""
        resp = self._send(self.box_addr, 0xFF, FN["GET_MEASURING_WHEEL"], bytes([0x01]))
        if len(resp) >= 9:
            return decode_measuring_wheel(resp[5:9])
        return None

    def _get_buffer_state(self):
        """Returns the raw buffer_state byte (0=middle, 1=full, 2=empty -
        see docs/PROTOCOL.md), or None if no valid reply."""
        resp = self._send(self.box_addr, 0xFF, FN["GET_BUFFER_STATE"])
        if len(resp) >= 6:
            return resp[5]
        return None

    def _extrude_material_handoff(self, gcmd, slot):
        """The real firmware's stage 6->7 handoff, ported 2026-08-17 from
        decompiled reference (extrude_material(), previously unreadable
        via decompyle3 - only recovered via raw bytecode disassembly, see
        FINDINGS.md). This is the moment the box has pushed filament up to
        the toolhead sensor and the toolhead extruder needs to actually
        grab it and pull the rest of the way, while the box keeps feeding
        - our earlier single-shot version (one slow toolhead move, trust
        the sensor) was missing this whole retry+verify structure.

        Sequence: a fixed priming move + stage 6, then up to
        extrude_material_times attempts of a short fast push
        (extrude_material_len_for_extruder mm at F12000), each verified
        against the ACTUAL distance the box's measuring wheel reports
        moving (not just "does the sensor still see filament") - stopping
        as soon as the buffer isn't full or the wheel confirms enough
        distance. Each attempt that doesn't look successful also tries
        stage 7 and, if that comes back bad, does a toolhead+box retreat
        before the next attempt.

        Returns True if the handoff looks like it succeeded, False
        otherwise (caller should treat False as "check physically", not
        as a hard failure - this is a faithful port of decompiled logic,
        not independently verified byte-for-byte against live hardware
        yet)."""
        self.gcode.run_script_from_command("M83")
        self.gcode.run_script_from_command("G0 E%.2f F35" % self.prime_e1)
        self._pause(0.3)
        resp6 = self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x06, 0x00]))
        status6 = resp6[3] if len(resp6) >= 4 else None
        if status6 != 0x00:
            gcmd.respond_info("CFS_EXTRUDE: stage 6 status=%s (continuing - real "
                               "firmware doesn't hard-stop here either)" %
                               (hex(status6) if status6 is not None else "no reply"))

        initial_distance = self._get_measuring_wheel()
        retry_count = 0
        for attempt in range(self.extrude_material_times):
            self.gcode.run_script_from_command("M83")
            self.gcode.run_script_from_command(
                "G0 E%.2f F12000" % self.extrude_material_len_for_extruder)
            self.gcode.run_script_from_command("M400")

            buffer_state = self._get_buffer_state()
            new_distance = self._get_measuring_wheel()
            diff_length = None
            if initial_distance is not None and new_distance is not None:
                diff_length = new_distance - initial_distance
            gcmd.respond_info(
                "CFS_EXTRUDE: handoff attempt %d/%d - buffer_state=%s, "
                "measuring-wheel diff=%s" % (
                    attempt + 1, self.extrude_material_times, buffer_state,
                    ("%.2fmm" % diff_length) if diff_length is not None else "unknown"))

            if buffer_state != 1 and (
                    diff_length is None or diff_length >= self.extrude_material_len_for_extruder):
                return True

            self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))
            self.gcode.run_script_from_command("M83")
            self.gcode.run_script_from_command("G0 E%.2f F10" % self.prime_e2)
            resp7 = self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x07, 0x02]))
            status7 = resp7[3] if len(resp7) >= 4 else None
            if status7 != 0x00:
                retry_count += 1
                if retry_count > 3:
                    gcmd.respond_info("CFS_EXTRUDE: handoff giving up - "
                                       "%d stage-7 failures" % retry_count)
                    return False
                # Recovery: check in with a generic retrude, then retreat
                # both the toolhead and the box before the next attempt -
                # matches the real firmware's own fallback here.
                recover = self._send(self.box_addr, 0xFF, FN["RETRUDE_PROCESS"],
                                      bytes([0x00, 0x00]))
                recover_status = recover[3] if len(recover) >= 4 else None
                if recover_status in (0x00, 0x12):
                    self.gcode.run_script_from_command("M83")
                    self.gcode.run_script_from_command("G0 E-10 F180")
                    self.gcode.run_script_from_command("M400")
                    self._send(self.box_addr, 0xFF, FN["MOVE_DISTANCE"], bytes([0x01, 50]))

        gcmd.respond_info("CFS_EXTRUDE: handoff did not confirm success after "
                           "%d attempts - check physically" % self.extrude_material_times)
        return False

    def cmd_CFS_SET_PRE_LOADING(self, gcmd):
        action = gcmd.get("ACTION", "CLOSE").upper()
        action_map = {"CLOSE": 0x00, "OPEN": 0x01, "RUN": 0x02, "TIGHT": 0x03}
        if action not in action_map:
            raise gcmd.error("ACTION must be one of CLOSE, OPEN, RUN, TIGHT")
        mask = gcmd.get_int("MASK", 0x0F, minval=0, maxval=15)
        timeout = 45.0 if action in ("RUN", "TIGHT") else 2.0
        resp = self._send(self.box_addr, 0xFF, FN["SET_PRE_LOADING"],
                           bytes([mask, action_map[action]]), timeout=timeout)
        gcmd.respond_info("CFS_SET_PRE_LOADING ACTION=%s MASK=%#04x: %s" % (
            action, mask, resp.hex() if resp else "(no reply)"))

    def cmd_CFS_STATUS(self, gcmd):
        if not self.addressed:
            gcmd.respond_info("CFS box not addressed (no response at klippy:connect) - "
                               "try CFS_RECONNECT")
            return
        bitmask = self.last_status.get("material_bitmask")
        if bitmask is not None:
            loaded = [name for name, bit in SLOT_BYTES.items() if bitmask & bit]
            gcmd.respond_info("CFS: material loaded in slots: %s" % (", ".join(loaded) or "none"))
        else:
            gcmd.respond_info("CFS: no status polled yet")

    def cmd_CFS_RECONNECT(self, gcmd):
        # Close and reopen the serial connection first, not just retry on
        # the existing one - a serial object that's been open since a
        # failed first attempt at klippy:connect can apparently get stuck
        # in a way a plain retry on the same connection doesn't recover
        # from (matches this project's live experience: the standalone
        # cfs_cli.py tool, which opens a fresh connection every time,
        # kept working throughout even when this extra's persistent one
        # didn't - see FINDINGS.md).
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        try:
            self._discover_and_address()
        except Exception as e:
            raise gcmd.error("CFS_RECONNECT failed: %s" % (e,))
        if self.addressed:
            gcmd.respond_info("CFS_RECONNECT: addressed OK")
        else:
            gcmd.respond_info("CFS_RECONNECT: still not addressed - box may not be "
                               "responding right now, check it physically")

    def cmd_CFS_SYNC_FEED(self, gcmd):
        """DIAGNOSTIC, added 2026-08-17 - not part of the normal
        CFS_EXTRUDE flow. Live testing found box-side pushes (verified via
        the measuring wheel) and toolhead-side pulls each individually
        "work", but filament still doesn't reliably make it out the
        nozzle - user's hypothesis: the box and toolhead extruder aren't
        actually moving AT THE SAME TIME, since our stage-based sequence
        sends a box command, waits for its reply, THEN sends a toolhead
        move - never truly concurrent. This command fires the box's raw
        MOVE_DISTANCE (fn 0x31 - a direct feed-motor move, not the full
        EXTRUDE_PROCESS state machine) with a short timeout (don't block
        waiting on its reply) immediately followed by a toolhead G1 E move
        of the same distance, to get them physically overlapping in time
        as closely as this extra's blocking, single-threaded architecture
        allows. CFS_SYNC_FEED DIST=<mm, 1-255, default 100>."""
        dist = gcmd.get_int("DIST", 100, minval=1, maxval=255)
        self.gcode.run_script_from_command("M83")
        self._send(self.box_addr, 0xFF, FN["MOVE_DISTANCE"],
                    bytes([0x00, dist & 0xFF]), timeout=0.3)
        self.gcode.run_script_from_command("G1 E%d F300" % dist)
        self.gcode.run_script_from_command("M400")
        gcmd.respond_info("CFS_SYNC_FEED: sent box MOVE_DISTANCE FORWARD %dmm "
                           "+ toolhead G1 E%d together - check physically" % (dist, dist))

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
                if status != 0x00:
                    # Either no reply, or the box thinks its part might
                    # already be done (this generic, no-specific-slot
                    # check-in can be a stale/no-op query if the earlier
                    # slot-specific RETRUDE_PROCESS calls above already
                    # finished the actual unload - expected and fine).
                    # The real signal is the toolhead sensor, not this
                    # call's status alone.
                    self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))
                    detected = self._toolhead_filament_detected()
                    if detected is False:
                        gcmd.respond_info("CFS_RETRUDE: toolhead sensor clear, "
                                           "unload complete (stopped early)")
                        return True
                    if status is None:
                        gcmd.respond_info("CFS_RETRUDE: no reply from box AND toolhead "
                                           "sensor still sees filament, stopping - "
                                           "check physically")
                        return False
                    # Sensor still sees filament but box did reply - keep
                    # going with the remaining steps.
            self.gcode.run_script_from_command("G0 E%.2f F%.0f" % (dist, speed))
            self.gcode.run_script_from_command("M400")
        detected = self._toolhead_filament_detected()
        return detected is False

    def cmd_CFS_RETRUDE(self, gcmd):
        slot_letter = gcmd.get("SLOT", "A").upper()
        if slot_letter not in SLOT_BYTES:
            raise gcmd.error("SLOT must be one of A, B, C, D")
        slot = SLOT_BYTES[slot_letter]

        self._reset_pre_loading()
        self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))
        self._pause(0.3)
        self._send(self.box_addr, 0xFF, FN["RETRUDE_PROCESS"], bytes([slot, 0x00]))
        self._pause(0.5)
        self._send(self.box_addr, 0xFF, FN["RETRUDE_PROCESS"], bytes([slot, 0x01]))

        ok = self._retrude_with_tip_form(gcmd)
        gcmd.respond_info("CFS_RETRUDE slot=%s complete (tip-form unload %s)" % (
            slot_letter, "confirmed clear" if ok else "did NOT confirm clear - check physically"))

    def cmd_CFS_EXTRUDE(self, gcmd):
        # STATUS 2026-08-16/17: slot switching itself is confirmed working
        # live on all 4 slots via this command. What ISN'T yet confirmed
        # live is the stage 6/7 handoff rewrite in
        # _extrude_material_handoff() (2026-08-17, see its own docstring
        # and FINDINGS.md) - a faithful port of the real firmware's
        # retry+measuring-wheel-verified logic, replacing an earlier
        # single-shot version that reliably got filament to the toolhead
        # sensor but not reliably past the extruder gear and out the
        # nozzle. Test that specifically before trusting this for real
        # prints - see docs/TOOLCHANGE_TEST_PLAN.md.
        #
        # Below: history of how slot-switching itself got fixed (not yet
        # updated since it happened - after exhausting live guessing
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
        if None in (self.extrude_pos_x, self.extrude_pos_y, self.extrude_pos_z):
            raise gcmd.error(
                "CFS_EXTRUDE: extrude_pos_x/y/z are not set in [creality_cfs] - "
                "refusing to guess. A previous default here crashed a real "
                "toolhead into the printer frame (2026-08-16 safety incident, "
                "see this file's header comment). Calibrate a safe position by "
                "jogging there manually and watching closely first, the same "
                "way the cut/purge positions were calibrated - see "
                "docs/MANUAL.md - then set all three in printer.cfg.")

        self._reset_pre_loading()

        # Step 1: error-clear-equivalent (see note above - best effort only)
        status = self._send(self.box_addr, 0xFF, FN["GET_BOX_STATE"])
        if status:
            gcmd.respond_info("CFS_EXTRUDE: pre-run GET_BOX_STATE=%s" % status.hex())
        self._send(self.box_addr, 0xFF, FN["SET_BOX_MODE"], bytes([0x00, 0x01]))
        self._pause(0.3)

        # Step 2: BOX_GO_TO_EXTRUDE_POS equivalent.
        #
        # Split into three separate legs (Z, then XY, then Z) instead of
        # one diagonal G1 that moves all three axes at once - added after
        # the 2026-08-16 frame collision (see the safety incident note by
        # extrude_pos_x/y/z above). A single diagonal move's exact path
        # depends on wherever the toolhead happened to be beforehand, which
        # made it easy to accidentally sweep through solid stuff. This
        # doesn't make an uncalibrated position safe - it only avoids
        # *extra*, unpredictable diagonal shortcuts on top of whatever
        # calibrated position you've set. Whichever Z the toolhead is
        # already at when this leg 1 move starts is used as the travel
        # height for the XY leg - if that's not clear of obstacles on your
        # printer, raise Z manually to a known-clear height before calling
        # CFS_EXTRUDE.
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=CFS_EXTRUDE")
        self.gcode.run_script_from_command("G90")
        self.gcode.run_script_from_command(
            "G1 Z%.2f F%.0f" % (self.extrude_pos_z, self.extrude_move_speed))
        self.gcode.run_script_from_command("M400")
        self.gcode.run_script_from_command(
            "G1 X%.2f Y%.2f F%.0f" % (
                self.extrude_pos_x, self.extrude_pos_y, self.extrude_move_speed))
        self.gcode.run_script_from_command("M400")

        self._send(self.box_addr, 0xFF, FN["CTRL_CONNECTION_MOTOR_ACTION"], bytes([0x01]))
        self._pause(0.5)
        self._send(self.box_addr, 0xFF, FN["TIGHTEN_UP_ENABLE"], bytes([0x01]))
        self._pause(0.3)

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
        self._pause(0.3)
        self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x04, 0x00]))
        self._pause(0.3)

        for _ in range(polls):
            self._send(self.box_addr, 0xFF, FN["EXTRUDE_PROCESS"], bytes([slot, 0x05, 0x00]))
            self._pause(0.4)

        # Stage 6/7 handoff - see _extrude_material_handoff()'s own docstring
        # for the full story (ported 2026-08-17 from decompiled reference,
        # replacing an earlier single-shot version that didn't verify
        # against the measuring wheel and had no retry logic).
        handoff_ok = self._extrude_material_handoff(gcmd, slot)
        if not handoff_ok:
            gcmd.respond_info("CFS_EXTRUDE: handoff did not confirm success - "
                               "check physically before printing")
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
        gcmd.respond_info("CFS_EXTRUDE slot=%s complete (%d polls, handoff %s)" % (
            slot_letter, polls, "confirmed" if handoff_ok else "NOT confirmed - check physically"))


def load_config(config):
    return CrealityCFS(config)
