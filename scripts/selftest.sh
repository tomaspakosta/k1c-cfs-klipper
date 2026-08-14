#!/bin/sh
# ============================================================================
# k1c-cfs-klipper self-test / setup helper
#
# Run this ON THE PRINTER (over SSH), as root.
#
# What it does — and does NOT do:
#   - Detects your Python interpreter and checks pyserial is importable
#   - Finds the CFS box's USB-serial device automatically (CH340/CH341,
#     idVendor 1a86) instead of you hunting for /dev/ttyUSB0 by hand
#   - Downloads this repo's cfs_protocol.py to a working directory
#   - Runs a READ-ONLY self-test: discovery, addressing, sensor/RFID/
#     version reads. NOTHING in this script ever calls RETRUDE, EXTRUDE,
#     or any command that moves a motor. That's a deliberate limit, not
#     an oversight - see the repo README's Safety section for why.
#   - Reports basic environment info useful for filing an issue if
#     something doesn't work (OS/kernel, Python version, Klipper/Moonraker
#     versions if reachable, whether an existing box.py is present)
#   - Safe to re-run any time; doesn't modify printer.cfg or install
#     anything into Klipper itself (that's the klipper_extra/ step, and
#     it's a separate, manual, deliberately-not-automated install - see
#     klipper_extra/README.md)
#
# Usage:
#   Recommended (review first):
#     scp scripts/selftest.sh root@<printer-ip>:/tmp/
#     ssh root@<printer-ip> sh /tmp/selftest.sh
#
#   Or, if you already trust it after reading it once:
#     ssh root@<printer-ip> "curl -sL https://raw.githubusercontent.com/tomaspakosta/k1c-cfs-klipper/master/scripts/selftest.sh | sh"
# ============================================================================

set -eu

REPO_RAW="https://raw.githubusercontent.com/tomaspakosta/k1c-cfs-klipper/master"
WORKDIR="/usr/data/k1c-cfs-klipper"
PASS=0
FAIL=0
WARN=0

pass() { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }

echo "=== k1c-cfs-klipper self-test ==="
echo "(read-only checks only - no motor will move)"
echo

# --- 1. environment info -----------------------------------------------
echo "--- Environment ---"
uname -a || true
[ -f /etc/creality_version ] && echo "Creality firmware: $(cat /etc/creality_version)" || true
echo

# --- 2. python + pyserial -----------------------------------------------
echo "--- Python / pyserial ---"
PYTHON=""
for candidate in /usr/share/klippy-env/bin/python python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
        if "$candidate" -c "import serial" >/dev/null 2>&1; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -n "$PYTHON" ]; then
    pass "found working Python with pyserial: $PYTHON ($($PYTHON --version 2>&1))"
else
    fail "no Python with pyserial found (tried /usr/share/klippy-env/bin/python, python3, python)"
    echo "  On stock Creality/Guilouz Helper-Script K1/K1C firmware, klippy-env"
    echo "  already has pyserial (Klipper's MCU comms use it) - if this failed,"
    echo "  double check the path above matches your install."
fi
echo

# --- 3. find the CFS USB-serial device -----------------------------------
echo "--- CFS USB device ---"
CFS_PORT=""
if [ -e /dev/serial/by-id ]; then
    for dev in /dev/serial/by-id/*1a86*; do
        [ -e "$dev" ] || continue
        CFS_PORT=$(readlink -f "$dev")
        break
    done
fi
if [ -z "$CFS_PORT" ]; then
    # fall back: any ttyUSB* at all (best-effort, less certain)
    for dev in /dev/ttyUSB*; do
        [ -e "$dev" ] || continue
        CFS_PORT="$dev"
        warn "no CH340 (1a86) device found by USB ID, falling back to first ttyUSB device: $dev - verify this is really the CFS"
        break
    done
fi

if [ -n "$CFS_PORT" ]; then
    pass "found CFS-looking serial device: $CFS_PORT"
else
    fail "no /dev/ttyUSB* or /dev/serial/by-id/*1a86* device found - is the CFS plugged in and powered?"
fi
echo

# --- 4. fetch cfs_protocol.py --------------------------------------------
# NOTE: on-device curl/wget on this class of Buildroot firmware is often
# built without SSL support at all ("Please recompile WITH_SSL") - HTTPS
# fetches from GitHub will simply never work there, no flag combination
# fixes that. So this is best-effort only; if it fails, that's expected on
# a lot of setups, not a real problem - scp the file from your own machine
# instead (see the message below).
echo "--- Fetching cfs_protocol.py ---"
mkdir -p "$WORKDIR"
FETCHED=0
if [ -f "$WORKDIR/cfs_protocol.py" ]; then
    pass "cfs_protocol.py already present at $WORKDIR (from a previous run or manual copy)"
    FETCHED=1
elif command -v curl >/dev/null 2>&1 && curl -s "$REPO_RAW/cfs_protocol.py" -o "$WORKDIR/cfs_protocol.py" 2>/dev/null; then
    pass "downloaded cfs_protocol.py to $WORKDIR"
    FETCHED=1
elif command -v wget >/dev/null 2>&1 && wget -q "$REPO_RAW/cfs_protocol.py" -O "$WORKDIR/cfs_protocol.py" 2>/dev/null; then
    pass "downloaded cfs_protocol.py to $WORKDIR"
    FETCHED=1
fi
if [ "$FETCHED" -eq 0 ]; then
    rm -f "$WORKDIR/cfs_protocol.py" 2>/dev/null || true
    warn "couldn't fetch cfs_protocol.py automatically (this device's curl/wget likely lacks HTTPS/SSL support - common on this firmware, not a bug in your setup)"
    echo "  From your own machine instead, run:"
    echo "    scp cfs_protocol.py root@<printer-ip>:$WORKDIR/cfs_protocol.py"
    echo "  then re-run this script."
fi
echo

# --- 5. existing CFS software check (informational, not pass/fail) ------
# NOTE: a naive "find any file named box.py" false-positives on unrelated
# vendored libraries that happen to share the name (e.g. the "rich"
# terminal library ships its own box-drawing box.py under pip's vendored
# packages, on multiple mount points on this firmware). We specifically
# look under klippy/extras/ paths only, and skip site-packages, to avoid
# that. Confirmed against a real printer with no CFS support: this
# correctly reports "not found" where the naive version incorrectly
# reported "found".
echo "--- Existing CFS software (informational) ---"
BOX_HITS=$(find / -path "*/klippy/extras/box.py" -not -path "*/site-packages/*" 2>/dev/null || true)
if [ -n "$BOX_HITS" ]; then
    echo "  Found what looks like an existing CFS box.py Klipper extra:"
    echo "$BOX_HITS" | sed 's/^/    /'
    echo "  You may already have official CFS support and might not need this"
    echo "  project - but note Creality only ships this compiled, so you can't"
    echo "  easily tell its exact behavior from the file alone."
else
    echo "  No klippy/extras/box.py found - matches the situation this project"
    echo "  was built for (see repo README's 'Why does this exist')."
fi
echo

# --- 6. read-only protocol self-test -------------------------------------
echo "--- Read-only protocol self-test ---"
if [ -n "$PYTHON" ] && [ -n "$CFS_PORT" ] && [ -f "$WORKDIR/cfs_protocol.py" ]; then
    "$PYTHON" - "$CFS_PORT" "$WORKDIR" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[2])
from cfs_protocol import CFSClient

port = sys.argv[1]
try:
    with CFSClient(port) as cfs:
        resp = cfs.discover()
        if not resp or len(resp) < 20:
            print("  [FAIL] no CFS box responded to discovery broadcast")
            sys.exit(1)
        uid = resp[7:19]
        print(f"  [PASS] discovered CFS box, UID={uid.hex()}")

        cfs.assign_address(uid, 0x01)
        print("  [PASS] address assignment sent")

        bitmask = cfs.get_filament_sensor_bitmask(0x01, bank=0x00)
        if bitmask is not None:
            slots = [n for n, b in zip("ABCD", (1, 2, 4, 8)) if bitmask & b]
            print(f"  [PASS] read filament sensor bitmask: {bitmask:#04x} "
                  f"(loaded: {slots or 'none'})")
        else:
            print("  [WARN] filament sensor query returned no usable data")

        version = cfs.get_version_sn(0x01)
        print(f"  [PASS] version/serial: {version}" if version else
              "  [WARN] version query returned no usable data")
except Exception as e:
    print(f"  [FAIL] self-test raised an exception: {e}")
    sys.exit(1)
PYEOF
else
    echo "  [SKIP] earlier checks failed - fix those first"
fi
echo

# --- summary ---------------------------------------------------------------
echo "=== Summary: $PASS passed, $WARN warnings, $FAIL failed ==="
if [ "$FAIL" -eq 0 ]; then
    echo "Looks good. Safe next step: try examples/02_retrude.py from the repo,"
    echo "supervised, watching the printer - see the main README."
else
    echo "Fix the FAILs above before trying anything that moves a motor."
fi
