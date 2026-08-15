# Manual: setup and usage, start to finish

This is the full walkthrough — connect the hardware, verify it, try safe
commands, then (optionally) install permanent Klipper integration. Each
step says clearly whether it's read-only or moves a motor.

If you just want the short version, see the main [README](../README.md)'s
Quick start. This page is for when you want the whole path spelled out, or
you're troubleshooting something specific.

## What you need

- A K1C (or similar) with a CFS box and a spare USB-A port
- SSH/root access to the printer
- The CFS connected via its USB cable (CH340/CH341 adapter) into any free
  USB-A port — no board mods needed, see
  [How it fits together](../README.md#how-it-fits-together) in the README
- `pyserial` available to whichever Python you'll use (already present in
  stock K1/K1C `klippy-env` — Klipper's own MCU comms use it)

## Step 1 — Connect and verify (read-only)

Plug the CFS's USB cable into the printer. On Linux this needs no driver
install; give it a few seconds to enumerate.

Copy [`scripts/selftest.sh`](../scripts/selftest.sh) onto the printer and
run it there:

```bash
scp scripts/selftest.sh root@<printer-ip>:/tmp/
ssh root@<printer-ip> sh /tmp/selftest.sh
```

This is entirely read-only. It checks Python/pyserial, finds the CFS's
serial device automatically, and runs a live discovery/addressing/sensor
self-test. If everything shows `[PASS]`, you're ready for step 2.

If it can't download `cfs_protocol.py` over HTTPS (common — a lot of this
class of printer firmware ships curl without SSL support at all), it'll
tell you to `scp` the file over manually and re-run — that's expected on
many setups, not a sign anything is wrong.

**If something fails here:** stop and fix it before moving on. Nothing
past this point will work if the basic connection doesn't.

## Step 2 — Explore with the CLI (read-only)

From the same directory the self-test left `cfs_protocol.py` in (default
`/usr/data/k1c-cfs-klipper` on the printer), copy `cfs_cli.py` there too,
then:

```bash
python cfs_cli.py status
```

You should see the box's version/serial, which slots have material
loaded, and buffer state. If this matches what you can see by eye (which
slots actually have spools in them), the protocol layer is working
correctly end to end.

## Step 3 — Try a motor command, supervised

**Read the Safety section in the main README before this step.** Watch
the printer the whole time.

```bash
python cfs_cli.py retrude --slot A
```

(Swap `A` for whichever slot actually has filament loaded.) This reels
filament back onto the spool — it's the "safest" motor operation to start
with, since it doesn't push material anywhere. You should see/hear the
spool motor turn briefly.

Once that works, `extrude --slot A` feeds filament forward — read the
warning printed before it runs.

If you get this far and it all matches what's described, your specific
CFS box speaks the same protocol we validated on ours. If something
*doesn't* match, please open an issue with what you saw — protocol
variations across firmware/board revisions are the most useful thing to
learn about.

## Step 4 — Optional: install permanent Klipper integration

This turns the standalone scripts into real Klipper gcode commands
(`CFS_STATUS`, `CFS_RETRUDE`, `CFS_EXTRUDE`) plus a background status poll
and Fluidd/Mainsail sensor display, instead of running Python scripts by
hand each time.

**Status reminder:** the protocol calls are the same validated ones from
steps 1-3. The Klipper *integration* code itself (config parsing, gcode
registration, the reactor timer) is a separate thing that needs testing on
your system - see [`klipper_extra/README.md`](../klipper_extra/README.md)
for the full caveat.

1. Copy the file:
   ```bash
   cp klipper_extra/creality_cfs.py <klipper-repo>/klippy/extras/creality_cfs.py
   ```
2. Add to `printer.cfg`:
   ```ini
   [creality_cfs]
   serial: /dev/ttyUSB0
   baud: 230400
   box_addr: 1
   ```
   (Use the exact port `selftest.sh`/`cfs_cli.py` reported in step 1-2.)
3. Restart Klipper (`RESTART` in the console, or via Fluidd/Mainsail's
   restart button).
4. Test read-only first: send `CFS_STATUS` from the console.
5. Check Fluidd/Mainsail's filament sensor panel — you should see `CFS_A`
   through `CFS_D` alongside any other filament sensors, updating live.
6. Only then try `CFS_RETRUDE SLOT=A`, watching the printer.

If Klipper fails to start after adding the config section, check
`klippy.log` for the error, remove the `[creality_cfs]` section to get
back to a working state, and open an issue with what you saw.

## Step 5 — Optional, advanced: the cutter

This is the part with the most physical risk (a real blade, toolhead
motion near it) and the least automation maturity in this repo so far —
[`macros/cut_macro_draft.cfg`](../macros/cut_macro_draft.cfg) is a
**written-but-never-run draft**. Do not load it and trigger it unattended.

Before using any cut automation:

1. **Hand-test your cutter mechanism first**, independent of any G-code —
   confirm it actually cuts cleanly when triggered manually. Don't assume
   your hardware behaves like ours (ours is lever-actuated; other
   revisions may drag filament across a stationary edge instead — see
   `docs/PROTOCOL.md`'s cutter section).
2. **Find your own real cut-position coordinates.** Ours (currently
   `X=36, Y=227` — it changed once already after hardware handling, from
   an original `X=150, Y=225`) are specific to our unit and are *not* a
   value you can safely reuse, and not something to treat as permanent
   even on the same unit. Home the printer, then jog by hand via the
   touchscreen/Fluidd (not with steppers disabled — that loses position
   tracking) until the mechanism triggers, then read the coordinates
   back from Klipper. Re-check after any physical handling of the
   printer - if a nearby purge/wipe flap exists, physically distinguish
   it from the cutter lever before trusting either one's coordinates
   (easy to confuse the two, we did more than once). Full walkthrough
   and a `G90`/`G91` gotcha we hit are documented in `docs/PROTOCOL.md`.
3. Only then consider adapting `cut_macro_draft.cfg` with your own
   coordinates, and test it supervised, at low speed, one pass at a time.

## Troubleshooting

**`selftest.sh` can't find a serial device.** Check the CFS is powered and
the USB cable is fully seated. `dmesg | tail` right after plugging it in
should show a `ch341` line — if it doesn't, the adapter isn't being
detected at the USB level at all, which points at the cable/connector
rather than anything in this repo.

**Discovery finds nothing / times out.** Confirm the port from
`selftest.sh` isn't already held open by something else (only one process
can have the serial port open at a time — this conflicts with Klipper's
own `[creality_cfs]` extra if that's already loaded and connected).

**Commands return errors you don't recognize.** Check the Response state
codes table in `docs/PROTOCOL.md` — if you hit one we haven't seen and
documented, that's valuable to report in an issue.

**`RETRUDE` jams with filament stuck in the toolhead extruder's own
drive gear** (needs a manual lever release, box reports
`RETRUDE_ERR2`/`RETRUDE_ERR7`). This can happen with filament that was
fed in deep by a completed `EXTRUDE`. `CFS_RETRUDE`/`cfs_cli.py retrude`
now run a "tip-forming" unload sequence automatically to avoid this
(see `docs/PROTOCOL.md`'s "RETRUDE — the tip-forming unload sequence")
— if you still hit it:
- **Never pull the filament by hand while a motor might still be
  running** (box or toolhead) — risks stripping the extruder's drive
  gear teeth. Let any in-progress command finish or time out first.
- Release the toolhead extruder's idler lever (disengages the drive
  gear) before pulling by hand — same technique used on Bambu AMS /
  Prusa MMU for exactly this failure mode.
- If pulling by hand still doesn't work even with the lever released,
  a "hot pull" can help: with the hotend still warm, let it cool toward
  ~90-120°C (PLA) / ~140-170°C (PETG/ABS) before pulling — filament
  that's firm-but-not-cold drags out cleanly, where fully molten
  filament can string/blob and re-catch, and fully cold filament can
  shear off instead of pulling free.

**Everything else** — see [Credits](../README.md#credits) in the main
README for the other projects this one builds on; if something looks like
a protocol difference rather than a bug here, one of those may have
already documented it for a different hardware revision.
