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
