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
import sys
import time

from cfs_protocol import CFSClient, SLOT_A, SLOT_B, SLOT_C, SLOT_D

SLOT_BYTES = {"A": SLOT_A, "B": SLOT_B, "C": SLOT_C, "D": SLOT_D}
BOX_ADDR = 0x01


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
        cfs.set_box_mode_idle(BOX_ADDR)
        time.sleep(0.3)
        cfs.retrude_stage(BOX_ADDR, slot, 0x00)
        time.sleep(0.5)
        cfs.retrude_stage(BOX_ADDR, slot, 0x01)
        print(f"RETRUDE slot={slot_letter} sent.")


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
        cfs.set_box_mode_idle(BOX_ADDR)
        time.sleep(0.3)
        cfs.ctrl_connection_motor(BOX_ADDR, action=0x01)
        time.sleep(0.5)
        cfs.tighten_up(BOX_ADDR, enable=True)
        time.sleep(0.3)
        cfs.extrude_stage(BOX_ADDR, slot, stage=0x00)
        time.sleep(0.3)
        cfs.extrude_stage(BOX_ADDR, slot, stage=0x04)
        time.sleep(0.3)
        for i in range(args.polls):
            resp = cfs.extrude_stage(BOX_ADDR, slot, stage=0x05)
            if args.verbose:
                print(f"  poll {i}: {resp.hex() if resp else '(no reply)'}")
            time.sleep(0.4)
        cfs.tighten_up(BOX_ADDR, enable=False)
        cfs.ctrl_connection_motor(BOX_ADDR, action=0x00)
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
    ns = argparse.Namespace(port=None, interactive=True, verbose=False)
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
    sub = p.add_subparsers(dest="command")

    sub.add_parser("status", help="read-only box/sensor status").set_defaults(
        func=do_status, interactive=False)

    p_retrude = sub.add_parser("retrude", help="reel filament back onto the spool")
    p_retrude.add_argument("--slot", default="A")
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
