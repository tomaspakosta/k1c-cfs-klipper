#!/usr/bin/env python3
"""
Example 4: interactively map physical slots (A/B/C/D) to sensor bits.

We found bit0=A, bit1=B, bit2=C, bit3=D (physical left-to-right) on our own
hardware, empirically, by pulling filament out one slot at a time and
watching which bit cleared. This script repeats that process so you can
verify the same mapping on your own box - it's cheap, read-only, and worth
doing before relying on the SLOT_A..SLOT_D constants in cfs_protocol.py.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cfs_protocol import CFSClient  # noqa: E402

PORT = "/dev/ttyUSB0"
BOX_ADDR = 0x01


def read_bitmask(cfs):
    return cfs.get_filament_sensor_bitmask(BOX_ADDR, bank=0x00)


def main():
    with CFSClient(PORT) as cfs:
        print("This will read the filament sensor bitmask, then ask you to remove")
        print("filament from each slot one at a time (left to right) so you can see")
        print("which bit corresponds to which physical position.\n")

        bitmask = read_bitmask(cfs)
        print(f"Current bitmask: {bitmask:#04x} ({bitmask:04b}b)")
        input("Pull filament from the LEFTMOST slot, then press Enter...")
        bitmask = read_bitmask(cfs)
        print(f"Now: {bitmask:#04x} ({bitmask:04b}b) - note which bit disappeared\n")

        input("Pull filament from the NEXT slot, then press Enter...")
        bitmask = read_bitmask(cfs)
        print(f"Now: {bitmask:#04x} ({bitmask:04b}b)\n")

        input("Pull filament from the NEXT slot, then press Enter...")
        bitmask = read_bitmask(cfs)
        print(f"Now: {bitmask:#04x} ({bitmask:04b}b)\n")

        input("Pull filament from the LAST (rightmost) slot, then press Enter...")
        bitmask = read_bitmask(cfs)
        print(f"Now: {bitmask:#04x} ({bitmask:04b}b) - should be 0x00 if all 4 are empty")


if __name__ == "__main__":
    main()
