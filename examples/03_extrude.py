#!/usr/bin/env python3
"""
Example 3: EXTRUDE_PROCESS — push filament from the spool through the box,
past the buffer. This is the sequence that took the most trial and error
to get right (see docs/PROTOCOL.md for the full story) - the key missing
piece was CTRL_CONNECTION_MOTOR_ACTION, which has to run BEFORE
EXTRUDE_PROCESS to mechanically "connect" the target slot to the shared
feed path. Without it, EXTRUDE_PROCESS fails deterministically with
EXTRUDE_ERR8 / EXTRUDE_ERR10, or silently runs the wrong slot's motor.

Physically confirmed working: filament visibly pushes past the buffer.

This sequence gets material as far as "loaded past the buffer". It does
NOT drive filament all the way to a toolhead or operate a cutter - those
are separate, downstream mechanisms (see docs/PROTOCOL.md's notes on
stage 7 / the toolhead filament switch for what's still unverified there).

WARNING: this moves a real motor and pushes real material. Make sure the
target slot has filament loaded and the spool's pinch wheel / cover is
properly closed (an open cover can let the motor spin without actually
gripping the filament). Watch the printer while this runs.
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cfs_protocol import CFSClient, SLOT_A  # noqa: E402

PORT = None  # auto-detects a CH340-family adapter; set e.g. "/dev/ttyUSB0" or "COM5" to override
BOX_ADDR = 0x01
SLOT = SLOT_A  # change to SLOT_B / SLOT_C / SLOT_D as needed


def main():
    with CFSClient(PORT) as cfs:
        print(f"Using port: {cfs.port}")
        print("=== SET_BOX_MODE -> IDLE ===")
        cfs.set_box_mode_idle(BOX_ADDR)
        time.sleep(0.3)

        print("\n=== CTRL_CONNECTION_MOTOR_ACTION: connect the feed path (ACTION=EXTRUDE) ===")
        print("This is the step that's easy to miss - without it, EXTRUDE_PROCESS")
        print("either errors out or silently runs the wrong slot's motor.")
        cfs.ctrl_connection_motor(BOX_ADDR, action=0x01)
        time.sleep(0.5)

        print("\n=== TIGHTEN_UP_ENABLE ===")
        cfs.tighten_up(BOX_ADDR, enable=True)
        time.sleep(0.3)

        print(f"\n=== EXTRUDE_PROCESS on slot {SLOT:#04x} ===")
        print("Watch the printer now - filament should push through toward the buffer.")

        cfs.extrude_stage(BOX_ADDR, SLOT, stage=0x00)
        time.sleep(0.3)
        cfs.extrude_stage(BOX_ADDR, SLOT, stage=0x04)
        time.sleep(0.3)

        print("\n--- polling stage 5 (this is where the actual feed motion happens) ---")
        for i in range(10):
            resp = cfs.extrude_stage(BOX_ADDR, SLOT, stage=0x05)
            if len(resp) >= 9:
                telemetry = resp[5:9].hex()
                status = resp[3]
                print(f"  poll {i}: status={status:#04x} telemetry={telemetry}")
            else:
                print(f"  poll {i}: {resp.hex() if resp else '(no reply yet - may arrive on next poll)'}")
            time.sleep(0.5)

        print("\n=== TIGHTEN_UP_ENABLE off + CTRL_CONNECTION_MOTOR_ACTION STOP (cleanup) ===")
        cfs.tighten_up(BOX_ADDR, enable=False)
        cfs.ctrl_connection_motor(BOX_ADDR, action=0x00)

        print("\nDone. If the telemetry bytes above were changing between polls "
              "(not stuck on one value) and status stayed 0x00, the motor really ran.")


if __name__ == "__main__":
    main()
