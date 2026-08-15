# Klipper extra

`creality_cfs.py` wraps the protocol validated elsewhere in this repo into
a real Klipper extra — gcode commands and a background status poll,
instead of standalone scripts you run by hand.

**Status: installed and loading cleanly in a real Klipper (2026-08-16),
but not yet usable end-to-end — see "Known issue" below.** The extra
itself loads without config errors, registers all its gcode commands
(confirmed via `HELP`), and the underlying protocol calls are the same
ones validated live elsewhere in this repo. What doesn't work yet: the
box never gets marked as addressed inside this extra (see below), so
`CFS_STATUS`/`CFS_RETRUDE`/`CFS_EXTRUDE` aren't usable as-is. `cfs_cli.py`
(the standalone tool) remains the reliable way to actually drive the box
for now.

It also has a known architectural limitation, documented in the file's own
header comment: it uses blocking serial calls from gcode command handlers,
which isn't the ideal way to integrate with Klipper's single-threaded
reactor. Fine for occasional manual commands; not yet built the "right" way.

## ⚠️ Known issue: box never gets addressed inside this extra

Confirmed live: the box responds reliably to `cfs_cli.py status` (a
fresh connection opened and closed each time) but this extra's own
discovery attempt at `klippy:connect` - the *exact same wire request* -
consistently gets no reply, even seconds apart on the same box. Tried
and didn't fix it: a `CFS_RECONNECT` command that closes/reopens the
serial connection and retries; a 3-attempt retry loop with a fresh
connection each try (all 3 attempts failed identically, confirmed in
`klippy.log`). The box is not the problem - something about this
extra's execution context (a persistent connection, inside Klipper's
reactor/greenlet framework) differs from the standalone tool's
(fresh connection, plain synchronous script) in a way that matters here.
**Not yet root-caused** - see `FINDINGS.md` in the private research log
for the full diagnostic trail and what to try next (byte-level TX/RX
logging inside `_send()` is the leading next step).

## Two real Klipper gotchas found installing this, worth knowing regardless of this project

1. **Never use Jinja2 `{# comment #}` syntax inside a macro's `gcode:`
   block.** Klipper's config loader strips everything after the *first*
   `#` on every raw line - including inside a multi-line `gcode:` value -
   before Jinja2 ever sees it. A Jinja2 comment's own `#` characters trip
   this, silently truncating the line and producing a confusing
   `jinja2.exceptions.TemplateSyntaxError: unexpected 'end of template'`
   pointing nowhere near the real cause. Hit this in two of this repo's
   own macro drafts - both fixed by just removing the `{# #}` comments.
2. **On at least this printer's community firmware stack (Guilouz
   Helper-Script), Moonraker's `firmware_restart`/`restart` API calls did
   not reliably reload edited Python extras** - the `klippy.py` process
   ID stayed the same across "restarts", and code changes weren't picked
   up. What did work: `kill <klippy.py pid>` followed by
   `/etc/init.d/S55klipper_service start` (find the exact service name
   with `ls /etc/init.d/ | grep -i klip` on your unit). Worth checking
   for if your own extra edits don't seem to take effect after a normal
   restart.

## Install

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

Restart Klipper (see the gotcha above if changes don't seem to apply),
then test read-only first:

```
CFS_STATUS
```

If it says "not addressed", try `CFS_RECONNECT` - but see the known
issue above, this currently doesn't reliably help either. Only move on
to `CFS_RETRUDE SLOT=A` / `CFS_EXTRUDE SLOT=A` once status genuinely
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
