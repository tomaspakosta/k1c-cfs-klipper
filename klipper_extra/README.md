# Klipper extra (work in progress)

`creality_cfs.py` wraps the protocol validated elsewhere in this repo into
a real Klipper extra — gcode commands and a background status poll,
instead of standalone scripts you run by hand.

**Status: written, not yet loaded into a running Klipper.** The protocol
calls it makes (discovery, addressing, `RETRUDE_PROCESS`, `EXTRUDE_PROCESS`
with the `CTRL_CONNECTION_MOTOR_ACTION` precursor) are the same ones
validated live elsewhere in this repo — but this specific file, as a piece
of Klipper integration code (config parsing, gcode command registration,
reactor timer), hasn't been installed and tested yet. Review it before
trusting it, and test each command individually and supervised the first
time, the same way everything else in this repo was validated.

It also has a known architectural limitation, documented in the file's own
header comment: it uses blocking serial calls from gcode command handlers,
which isn't the ideal way to integrate with Klipper's single-threaded
reactor. Fine for occasional manual commands; not yet built the "right" way.

## Install (once you've reviewed it)

```bash
cp creality_cfs.py ~/klipper/klippy/extras/creality_cfs.py
```

Add to `printer.cfg`:

```ini
[creality_cfs]
serial: /dev/ttyUSB0
baud: 230400
box_addr: 1
```

Restart Klipper, then test read-only first:

```
CFS_STATUS
```

Only move on to `CFS_RETRUDE SLOT=A` / `CFS_EXTRUDE SLOT=A` once that
works and you're watching the printer.

**Switching slots ✅ works** — after a long debugging trail (see
[`docs/TOOLCHANGE_TEST_PLAN.md`](../docs/TOOLCHANGE_TEST_PLAN.md) phase
2), the fix turned out to be completing `EXTRUDE_PROCESS`'s full
sequence (stages 6/7 plus toolhead-side prime moves between them) and
marking the loaded slot via `SET_BOX_MODE`'s per-slot form at the end -
confirmed live, switching to a slot other than A for the first time all
project long. **Caveat: that confirmation was via `cfs_cli.py`
(standalone), not this Klipper extra itself** - the extra's
`cmd_CFS_EXTRUDE` was updated to match the same sequence but hasn't
been loaded into a running Klipper and exercised yet; treat it as
"should work, not yet independently verified as a Klipper extra." If
your printer's real extrude position differs from the factory default
used for the toolhead "go to extrude position" move (`X148 Y225.3
Z30`), set `extrude_pos_x` / `extrude_pos_y` / `extrude_pos_z` in
`[creality_cfs]`. `BOX_NOZZLE_CLEAN` and stage 7's exact 3rd payload
byte remain unconfirmed guesses, but didn't block the live result.

## Fluidd / Mainsail display

You don't need a custom panel for this. The extra registers one small
object per slot named `filament_switch_sensor CFS_A` (through `CFS_D`),
matching the exact shape Klipper's built-in filament sensor uses
(`filament_detected` + `enabled`). Fluidd and Mainsail already know how to
display any object with that name pattern in their normal filament-sensor
UI — so once this extra is loaded and polling, slots A–D should just show
up there like any other runout sensor, updating live as material is
loaded/unloaded. Change the `CFS_` prefix via `sensor_name_prefix:` in the
config if you want different names.

This part hasn't been visually confirmed in a real Fluidd session yet
(same "written, not yet tested" caveat as the rest of this file) — the
object shape and naming convention are correct per Klipper's own
`filament_switch_sensor` implementation, but it's worth a quick look the
first time you load this to confirm it renders as expected.
