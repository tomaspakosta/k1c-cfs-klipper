#!/usr/bin/env python3
"""
cfs_cli.py - one friendly entry point for the CFS protocol client.

Interactive (a menu, good for exploring / less technical use):
    python cfs_cli.py

Scriptable (good for automation, CI, your own tooling):
    python cfs_cli.py status
    python cfs_cli.py retrude --slot A
    python cfs_cli.py extrude --slot A --polls 20
    python cfs_cli.py map-slots

Every subcommand accepts --port to override auto-detection, e.g.:
    python cfs_cli.py status --port /dev/ttyUSB1

Motor-moving commands (retrude, extrude) always print a warning and, in
interactive mode, ask for confirmation before doing anything - see the
Safety section in the main README. Non-interactive (scripted) use skips
the confirmation prompt by design, since a script running unattended can't
answer one - that's on you to only automate once you've verified a command
by hand first.
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

from cfs_protocol import CFSClient, SLOT_A, SLOT_B, SLOT_C, SLOT_D, TIP_FORM_STEPS

SLOT_BYTES = {"A": SLOT_A, "B": SLOT_B, "C": SLOT_C, "D": SLOT_D}
BOX_ADDR = 0x01


def _moonraker_gcode(script, host, port=7125, timeout=45.0):
    """Send a G-code script to Klipper via Moonraker's HTTP API. Used by
    do_extrude() for the toolhead-side moves the real official firmware
    sequence does between EXTRUDE_PROCESS stages 5/6/7 - see that
    function's comment and FINDINGS.md in the private research log for
    where this came from. Only reachable when this script runs on (or
    can reach) the printer's own Moonraker - stdlib-only (no new
    dependency), matching this repo's "dependency-light" approach.

    Moonraker's /printer/gcode/script doesn't return until Klipper
    actually finishes the command, not just when it's queued - so the
    timeout has to cover the real move time, not just network latency.
    The slowest move we send here is G0 E5 F10 (5mm at 10mm/min = ~30s);
    the default here (45s) was bumped up from an original 10s after that
    undershoot crashed a live test outright (see FINDINGS.md, session
    2026-08-16 - the CFS box itself was fine, this was purely a client
    bug)."""
    url = "http://%s:%d/printer/gcode/script" % (host, port)
    data = json.dumps({"script": script}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _moonraker_sensor(host, sensor_name, port=7125, timeout=5.0):
    """Query one Moonraker printer object's status - used by
    retrude_with_tip_form() to check the toolhead filament sensor.
    Object names with spaces (e.g. "filament_switch_sensor
    filament_sensor_2") must be URL-encoded - a real bug hit live during
    testing (see FINDINGS.md, session 2026-08-16) before this used
    urllib.parse.quote()."""
    url = "http://%s:%d/printer/objects/query?%s" % (
        host, port, urllib.parse.quote(sensor_name))
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["result"]["status"][sensor_name]


def retrude_with_tip_form(cfs, box_addr, moonraker_host,
                           sensor_name="filament_switch_sensor filament_sensor_2",
                           verbose=False):
    """UNTESTED (as of this writing) reimplementation of the real official
    firmware's unload sequence - see TIP_FORM_STEPS in cfs_protocol.py and
    FINDINGS.md in the private research log for where this came from and
    why: a single box-side RETRUDE_PROCESS call (what this repo did
    before) can leave filament jammed in the toolhead extruder's own
    drive gear, needing a manual lever release - confirmed live, see
    docs/TOOLCHANGE_TEST_PLAN.md.

    First "wiggles" the extruder a small net distance to re-melt and
    re-shape the filament tip into a smooth taper - a blobby/snagged tip
    is what catches in the gear on the way out - then does the real
    retraction in -15mm chunks, checking in with the box (a generic,
    no-specific-slot RETRUDE_PROCESS call) and the toolhead sensor
    between chunks so it can stop as soon as filament is confirmed clear
    rather than always doing the full sequence.

    Returns True if it believes the unload succeeded (toolhead sensor
    confirmed clear, or the box acknowledged every step), False if the
    box reported a real failure partway through.
    """
    _moonraker_gcode("M83", moonraker_host)
    for dist, speed in TIP_FORM_STEPS:
        if dist <= -10:
            # A "real" retraction step - check in with the box first,
            # same as the official sequence.
            resp = cfs.retrude_stage(box_addr, 0x00, 0x00)  # slot=none, trigger=BUFFER
            status = resp[3] if len(resp) >= 4 else None
            if verbose:
                print(f"    box check-in before {dist}mm: "
                      f"{'status=%#04x' % status if status is not None else '(no reply)'}")
            if status != 0x00:
                # Either the box didn't reply at all, or it thinks its
                # part might already be done (this call is a generic,
                # no-specific-slot check-in - it can be a no-op/stale
                # query if the earlier slot-specific RETRUDE_PROCESS
                # calls in do_retrude() already finished the actual
                # unload, which is expected and fine). Either way, the
                # real signal is whether the toolhead sensor agrees -
                # don't declare failure on this call's status alone.
                cfs.set_box_mode_idle(box_addr)
                sensor = _moonraker_sensor(moonraker_host, sensor_name)
                if not sensor.get("filament_detected"):
                    if verbose:
                        print("    toolhead sensor clear - unload complete, stopping early")
                    return True
                if status is None:
                    print("    no reply from box AND toolhead sensor still sees "
                          "filament, stopping - check physically")
                    return False
                # Sensor still sees filament but box did reply - fall
                # through and keep trying the remaining steps.
        _moonraker_gcode("G0 E%.2f F%.0f" % (dist, speed), moonraker_host)
        _moonraker_gcode("M400", moonraker_host)
    sensor = _moonraker_sensor(moonraker_host, sensor_name)
    return not sensor.get("filament_detected")


def print_status(cfs):
    print(f"Using port: {cfs.port}")
    version = cfs.get_version_sn(BOX_ADDR)
    print(f"Box version/serial: {version}")

    bitmask = cfs.get_filament_sensor_bitmask(BOX_ADDR, bank=0x00)
    if bitmask is not None:
        loaded = [name for name, bit in zip("ABCD", (1, 2, 4, 8)) if bitmask & bit]
        print(f"Material loaded in slots: {', '.join(loaded) or 'none'} "
              f"(bitmask {bitmask:#04x})")
    else:
        print("Material sensor: no response")

    conn = cfs.get_filament_sensor_bitmask(BOX_ADDR, bank=0x01)
    if conn is not None:
        print(f"Connection sensor bitmask: {conn:#04x}")

    buf = cfs.send(BOX_ADDR, 0xFF, 0x05)
    if len(buf) >= 6:
        buf_val = buf[5]
        buf_meaning = {0x00: "middle", 0x01: "full", 0x02: "empty"}.get(buf_val, "unknown")
        print(f"Buffer state: {buf_val} ({buf_meaning})")


def do_status(args):
    with CFSClient(args.port) as cfs:
        print_status(cfs)


def do_retrude(args):
    slot_letter = args.slot.upper()
    if slot_letter not in SLOT_BYTES:
        print(f"error: --slot must be one of A, B, C, D (got {args.slot!r})", file=sys.stderr)
        sys.exit(1)

    print(f"About to RETRUDE slot {slot_letter} - this moves a real motor "
          f"and reels filament back onto the spool.")
    if args.interactive and not _confirm("Continue?"):
        print("cancelled")
        return

    with CFSClient(args.port) as cfs:
        print(f"Using port: {cfs.port}")
        slot = SLOT_BYTES[slot_letter]
        cfs.set_pre_loading(BOX_ADDR, "CLOSE")  # reset to known state, see FINDINGS.md
        time.sleep(0.2)
        cfs.set_box_mode_idle(BOX_ADDR)
        time.sleep(0.3)
        cfs.retrude_stage(BOX_ADDR, slot, 0x00)
        time.sleep(0.5)
        cfs.retrude_stage(BOX_ADDR, slot, 0x01)
        print(f"RETRUDE slot={slot_letter} sent.")

        if args.moonraker_host:
            print("Running tip-form unload sequence to clear the toolhead "
                  "extruder cleanly (see FINDINGS.md) - UNTESTED, watch closely.")
            ok = retrude_with_tip_form(cfs, BOX_ADDR, args.moonraker_host,
                                        verbose=args.verbose)
            print("Tip-form unload: %s" % ("confirmed clear" if ok else
                                            "did NOT confirm clear - check physically"))
        else:
            print("NOTE: --moonraker-host not set, skipping the tip-form "
                  "unload sequence - filament may jam in the toolhead "
                  "extruder's own grip without it, see FINDINGS.md.")


def do_extrude(args):
    slot_letter = args.slot.upper()
    if slot_letter not in SLOT_BYTES:
        print(f"error: --slot must be one of A, B, C, D (got {args.slot!r})", file=sys.stderr)
        sys.exit(1)

    print(f"About to EXTRUDE slot {slot_letter} - this moves a real motor "
          f"and feeds filament from the spool. Watch the printer.")
    if args.interactive and not _confirm("Continue?"):
        print("cancelled")
        return

    with CFSClient(args.port) as cfs:
        print(f"Using port: {cfs.port}")
        slot = SLOT_BYTES[slot_letter]
        cfs.set_pre_loading(BOX_ADDR, "CLOSE")  # reset to known state, see FINDINGS.md
        time.sleep(0.2)
        cfs.set_box_mode_idle(BOX_ADDR)
        time.sleep(0.3)
        cfs.ctrl_connection_motor(BOX_ADDR, action=0x01)
        time.sleep(0.5)
        cfs.tighten_up(BOX_ADDR, enable=True)
        time.sleep(0.3)
        r0 = cfs.extrude_stage(BOX_ADDR, slot, stage=0x00)
        if args.verbose:
            print(f"  stage 0: {r0.hex() if r0 else '(no reply)'}")
        time.sleep(0.3)
        r4 = cfs.extrude_stage(BOX_ADDR, slot, stage=0x04)
        if args.verbose:
            print(f"  stage 4: {r4.hex() if r4 else '(no reply)'}")
        time.sleep(0.3)
        for i in range(args.polls):
            resp = cfs.extrude_stage(BOX_ADDR, slot, stage=0x05)
            if args.verbose:
                print(f"  poll {i}: {resp.hex() if resp else '(no reply)'}")
            time.sleep(0.4)
        cfs.tighten_up(BOX_ADDR, enable=False)
        cfs.ctrl_connection_motor(BOX_ADDR, action=0x00)

        # UNTESTED: the rest of this function (stage 6/7 + the toolhead E
        # moves between them) was added after decompiling Creality's real
        # official firmware and reading its actual extrude_material()
        # sequence - see FINDINGS.md in the private research log for the
        # full story and the exact source this came from (not reproduced
        # here - see that log's legal/provenance note). We had NEVER done
        # any of this before: we always stopped right after the stage-5
        # poll loop above, which may be why switching to a slot other than
        # A never worked - the box may never have been told the load
        # actually finished. The real sequence, in order:
        #   M83, G0 E10 F35 (slow toolhead prime)  -> stage 6
        #   M83, G0 E5 F10  (slower toolhead prime) -> stage 7
        #   SET_BOX_MODE PRINT for this specific slot (not just IDLE)
        # Requires reaching this printer's Moonraker to send the toolhead
        # moves - skipped (with a warning) if --moonraker-host isn't set.
        if args.moonraker_host:
            _moonraker_gcode("M83", args.moonraker_host)
            _moonraker_gcode("G0 E10 F35", args.moonraker_host)
            time.sleep(0.3)
            ret6 = cfs.extrude_stage(BOX_ADDR, slot, stage=0x06)
            if args.verbose:
                print(f"  stage 6: {ret6.hex() if ret6 else '(no reply)'}")
            time.sleep(0.3)
            _moonraker_gcode("M83", args.moonraker_host)
            _moonraker_gcode("G0 E5 F10", args.moonraker_host)
            time.sleep(0.3)
            ret7 = cfs.extrude_stage(BOX_ADDR, slot, stage=0x07, amount=0x03)
            if args.verbose:
                print(f"  stage 7: {ret7.hex() if ret7 else '(no reply)'}")
            time.sleep(0.3)
            # Mark this specific slot as the active PRINT-mode slot - the
            # real official sequence does this, we never did.
            cfs.set_box_mode(BOX_ADDR, "PRINT", slot=slot)
        else:
            print("NOTE: --moonraker-host not set, skipping the stage 6/7 + "
                  "toolhead-prime completion (see the comment above this line "
                  "in cfs_cli.py) - slot switching may not work without it.")

        # A completed EXTRUDE_PROCESS run can leave the box reporting a
        # latched error status (seen live: EXTRUDE_ERR8) on GET_BOX_STATE
        # even when the extrude itself succeeded (toolhead sensor
        # confirmed). A fresh SET_BOX_MODE(IDLE) clears it - confirmed live
        # on real hardware, see docs/PROTOCOL.md.
        cfs.set_box_mode_idle(BOX_ADDR)
        print(f"EXTRUDE slot={slot_letter} complete ({args.polls} polls).")


def do_map_slots(args):
    with CFSClient(args.port) as cfs:
        print(f"Using port: {cfs.port}")
        print("This reads the filament sensor bitmask, then asks you to remove")
        print("filament from each slot one at a time (left to right) so you can")
        print("see which bit corresponds to which physical position.\n")

        def read():
            return cfs.get_filament_sensor_bitmask(BOX_ADDR, bank=0x00)

        bm = read()
        print(f"Current bitmask: {bm:#04x} ({bm:04b}b)")
        for label in ("LEFTMOST", "NEXT", "NEXT", "LAST (rightmost)"):
            input(f"Pull filament from the {label} slot, then press Enter...")
            bm = read()
            print(f"Now: {bm:#04x} ({bm:04b}b)\n")


def _confirm(prompt):
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer == "y"


def interactive_menu():
    print("=== k1c-cfs-klipper interactive CLI ===\n")
    options = [
        ("Status (read-only)", lambda: do_status(_ns())),
        ("Retrude a slot (moves a motor)", _interactive_retrude),
        ("Extrude a slot (moves a motor)", _interactive_extrude),
        ("Map physical slots to sensor bits (read-only, guided)", lambda: do_map_slots(_ns())),
        ("Quit", None),
    ]
    while True:
        print("\nWhat do you want to do?")
        for i, (label, _) in enumerate(options, 1):
            print(f"  {i}) {label}")
        try:
            choice = input("> ").strip()
        except EOFError:
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(options)):
            print("Please enter a number from the list.")
            continue
        idx = int(choice) - 1
        if options[idx][1] is None:
            break
        try:
            options[idx][1]()
        except Exception as e:
            print(f"Error: {e}")


def _ns(**kwargs):
    ns = argparse.Namespace(port=None, interactive=True, verbose=False,
                             moonraker_host="127.0.0.1")
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def _interactive_retrude():
    slot = input("Which slot? [A/B/C/D] ").strip().upper() or "A"
    do_retrude(_ns(slot=slot))


def _interactive_extrude():
    slot = input("Which slot? [A/B/C/D] ").strip().upper() or "A"
    polls_raw = input("How many stage-5 polls? [20] ").strip()
    polls = int(polls_raw) if polls_raw else 20
    do_extrude(_ns(slot=slot, polls=polls, verbose=True))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", default=None,
                    help="serial port, e.g. /dev/ttyUSB0 or COM5 (default: auto-detect)")
    p.add_argument("--moonraker-host", default="127.0.0.1",
                    help="Moonraker host for the toolhead moves EXTRUDE and RETRUDE "
                         "need to complete their sequences (default: 127.0.0.1, i.e. "
                         "run this script on the printer itself). Pass an empty "
                         "string to skip those moves entirely.")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("status", help="read-only box/sensor status").set_defaults(
        func=do_status, interactive=False)

    p_retrude = sub.add_parser("retrude", help="reel filament back onto the spool")
    p_retrude.add_argument("--slot", default="A")
    p_retrude.add_argument("--verbose", action="store_true")
    p_retrude.set_defaults(func=do_retrude, interactive=False)

    p_extrude = sub.add_parser("extrude", help="feed filament from the spool")
    p_extrude.add_argument("--slot", default="A")
    p_extrude.add_argument("--polls", type=int, default=20)
    p_extrude.add_argument("--verbose", action="store_true")
    p_extrude.set_defaults(func=do_extrude, interactive=False)

    sub.add_parser("map-slots", help="interactively map slots to sensor bits").set_defaults(
        func=do_map_slots, interactive=False)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        interactive_menu()
        return
    if not hasattr(args, "verbose"):
        args.verbose = False
    args.func(args)


if __name__ == "__main__":
    main()
