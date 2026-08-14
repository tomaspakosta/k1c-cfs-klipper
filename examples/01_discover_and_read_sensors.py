#!/usr/bin/env python3
"""
Example 1: discover the CFS box, assign it an address, and read its
sensors/state. Entirely read-only — no motor movement.

Run this first after plugging the CFS into a USB port to confirm your
setup is working before trying anything from example 2/3.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cfs_protocol import CFSClient  # noqa: E402

PORT = "/dev/ttyUSB0"  # adjust to your system - see README for how to find this
BOX_ADDR = 0x01


def main():
    with CFSClient(PORT) as cfs:
        print("=== Discovering CFS box ===")
        resp = cfs.discover()
        if not resp or len(resp) < 20:
            print("No box responded. Check wiring/power and try again.")
            return
        uid = resp[7:19]
        print(f"Found box, UID={uid.hex()}")

        print(f"\n=== Assigning address {BOX_ADDR:#04x} ===")
        cfs.assign_address(uid, BOX_ADDR)

        print("\n=== Box state ===")
        print(f"  raw: {cfs.get_box_state(BOX_ADDR).hex()}")

        print("\n=== Filament sensor bitmask (which slots have material) ===")
        bitmask = cfs.get_filament_sensor_bitmask(BOX_ADDR, bank=0x00)
        if bitmask is not None:
            slots = [name for name, bit in zip("ABCD", (1, 2, 4, 8)) if bitmask & bit]
            print(f"  bitmask={bitmask:#04x} ({bitmask:04b}b) -> loaded slots: {slots or 'none'}")

        print("\n=== Connection sensor bitmask (which slot is mechanically connected) ===")
        conn = cfs.get_filament_sensor_bitmask(BOX_ADDR, bank=0x01)
        if conn is not None:
            print(f"  bitmask={conn:#04x} ({conn:04b}b)")

        print("\n=== Version / serial ===")
        print(f"  {cfs.get_version_sn(BOX_ADDR)}")

        print("\n=== RFID (note: uses a plain 0-3 index, not the A/B/C/D bitmask -")
        print("    see the docstring on get_rfid() before reading too much into this) ===")
        for slot_index in range(4):
            rfid = cfs.get_rfid(BOX_ADDR, slot_index)
            print(f"  index {slot_index}: {rfid}")


if __name__ == "__main__":
    main()
