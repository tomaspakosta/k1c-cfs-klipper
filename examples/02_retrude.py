#!/usr/bin/env python3
"""
Example 2: RETRUDE_PROCESS — reel filament back onto the spool.

This is the "safe" motor operation to test first: it pulls material back
INTO the box, away from the buffer/toolhead, and has no dependency on
downstream hardware (no cutter or toolhead sensor needed).

Physically confirmed working: the spool visibly/audibly reels filament
back when this runs.

WARNING: this moves a real motor. Make sure filament is actually loaded in
the target slot, and watch the printer while this runs.
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cfs_protocol import CFSClient, SLOT_A  # noqa: E402

PORT = "/dev/ttyUSB0"
BOX_ADDR = 0x01
SLOT = SLOT_A  # change to SLOT_B / SLOT_C / SLOT_D as needed


def main():
    with CFSClient(PORT) as cfs:
        print("=== SET_BOX_MODE -> IDLE ===")
        cfs.set_box_mode_idle(BOX_ADDR)
        time.sleep(0.3)

        print(f"\n=== RETRUDE_PROCESS on slot {SLOT:#04x} ===")
        print("Watch the printer now - the motor should reel filament back onto the spool.")
        r1 = cfs.retrude_stage(BOX_ADDR, SLOT, 0x00)
        print(f"  stage 0: {r1.hex()}")
        time.sleep(0.5)
        r2 = cfs.retrude_stage(BOX_ADDR, SLOT, 0x01)
        print(f"  stage 1: {r2.hex()}")

        print("\nDone. Every response should show status byte 0x00 (OK) at position 3.")


if __name__ == "__main__":
    main()
