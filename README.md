# K1C CFS without official Creality firmware

Talk directly to a Creality CFS (multi-material filament box) from a K1C running
a **community Klipper stack**, with **zero dependency on Creality's official,
closed-source CFS firmware**. Discovery, addressing, sensors, RFID, and both
`RETRUDE` and `EXTRUDE` motor operations are working and physically verified
on real hardware.

If you're technical: jump to [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for the
full wire protocol and [`examples/`](examples/) for working scripts.
If you're not: keep reading, the next section explains what this actually is
and why it needed to be built at all.

## Why does this exist?

Creality's K1C can be fitted with a **CFS unit** — a box that holds up to 4
spools and feeds whichever one you need for multi-color printing. Officially,
that only works if your printer is running Creality's own firmware, which
includes a piece of software called `box.py` that knows how to talk to the
CFS box.

Two things get in the way of that for a lot of people:

1. **Creality only ships `box.py` as a compiled binary**, not source code —
   even though Klipper (which it's built on) is licensed under the GPL,
   which requires source to be available. This is a known, long-standing
   complaint in the Creality/Klipper community, not just our opinion — see
   [`fake-name/cfs-reverse-engineering`](https://github.com/fake-name/cfs-reverse-engineering)
   for the same conclusion from an independent project.
2. **Plenty of K1C owners aren't running Creality's official firmware at
   all.** A popular alternative is
   [Guilouz's Creality-Helper-Script](https://github.com/Guilouz/Creality-Helper-Script),
   which replaces Creality's stack with a more standard, community-maintained
   one (mainline-style Klipper, real Moonraker, root access, no telemetry).
   That's a genuinely better experience for a lot of use cases — but it never
   shipped with CFS support, because Creality never open-sourced the piece
   that talks to it.

That was exactly the situation we started from: a K1C on the board variant
`CR4CU220812S12` (the version that officially supports CFS) — but the
firmware itself only *identifies* as the older `S11` variant internally, and
the community Klipper stack it's running has **no trace of `box.py`
anywhere** — not in the live config, not in the ROM factory partition, not
in any backup. The config file even had a comment left behind: `# K1C -
Cleaned Macro Config (Without CFS)`. Someone had deliberately stripped it out
at some point.

So: physically capable hardware, a CFS unit sitting there ready to use, and
no software path to talk to it that didn't mean giving up the better,
community-maintained firmware. That's the gap this project fills.

## What we found instead

The CFS box doesn't need Creality's software to be controlled — it just
needs *something* that speaks its wire protocol correctly. That protocol was
never officially documented, but it didn't need to be reverse-engineered
completely from scratch either: a small but real community of people had
already been chipping away at it independently (see [Credits](#credits)).
We combined their partial results, cross-checked them against each other,
and then validated everything live against real hardware — starting with
pure read-only queries, and only moving on to commands that spin a motor
once we were confident in what we were sending.

**Confirmed working, live, on real hardware:**
- ✅ Discovering the box and assigning it an address over the bus
- ✅ Reading its status, firmware version, and filament sensors
- ✅ Reading RFID data per slot (works, though most non-Creality-branded
  filament spools don't have an RFID chip to read in the first place — that's
  expected, not a bug)
- ✅ **`RETRUDE`** — reeling filament back onto the spool
- ✅ **`EXTRUDE`** — feeding filament from the spool, through the box, past
  the internal buffer

**Not yet covered:** driving filament the rest of the way to the toolhead,
or operating the cutter — that needs the toolhead-side hardware reconnected,
which is a separate, ongoing part of this project (see
[Status / what's next](#status--whats-next)).

## Physical setup

The CFS box connects over **USB**, via a small USB↔RS485 adapter built into
its cable (CH340/CH341 chip). Plug it into any spare USB-A port on the
printer — on Linux, the kernel handles the rest automatically, no driver
install needed. It shows up as `/dev/ttyUSB0` (or similar).

## Quick start

```bash
pip install pyserial
python examples/01_discover_and_read_sensors.py
```

That script is entirely read-only — safe to run any time to sanity-check
your connection. From there:

- `examples/02_retrude.py` — reels filament back (moves a motor, but the
  "safe" one to start with)
- `examples/03_extrude.py` — feeds filament forward past the buffer (moves a
  motor; read the warning at the top of that file first)
- `examples/04_map_slots.py` — interactively confirms which physical slot
  (A/B/C/D) corresponds to which sensor bit on *your* box (ours came out
  left-to-right, but it's cheap to double-check)

All scripts default to `/dev/ttyUSB0` — edit the `PORT` constant near the
top of each file if yours enumerates differently.

## Status / what's next

This is **not** a Klipper extra (plugin) yet — it's a validated, working
protocol client, meant as the foundation for one. The plan:

1. Wrap this into a real Klipper extra with proper gcode commands and
   background state polling.
2. Reconnect the toolhead-side cutter and filament sensor (currently
   disassembled on our test unit) and work out the remaining piece needed to
   drive filament all the way to the toolhead — see the notes at the end of
   [`docs/PROTOCOL.md`](docs/PROTOCOL.md) for exactly where that picture is
   still incomplete.

Contributions, corrections, and hardware validation on other CFS/board
revisions are welcome — open an issue.

## Credits

This project stands on real work by other people, not just ours:

- [`ityshchenko/klipper-cfs`](https://github.com/ityshchenko/klipper-cfs) —
  the CRC8/frame-format implementation and a real captured traffic dump we
  validated against, byte for byte.
- [`fake-name/cfs-reverse-engineering`](https://github.com/fake-name/cfs-reverse-engineering) —
  hardware and firmware analysis of the CFS box itself.
- [`gitstonelabs/creality-cfs-klipper`](https://github.com/gitstonelabs/creality-cfs-klipper)
  and
  [`FrederickAlt/CREALITY-K1-AND-K1-MAX-CFS-RETRUDE-BEFORE-CUT-MOD`](https://github.com/FrederickAlt/CREALITY-K1-AND-K1-MAX-CFS-RETRUDE-BEFORE-CUT-MOD) —
  two independent, more mature reimplementations whose documentation is what
  finally unblocked the `EXTRUDE_PROCESS` sequence for us (see
  `docs/PROTOCOL.md` for exactly how).
- [Guilouz/Creality-Helper-Script](https://github.com/Guilouz/Creality-Helper-Script) —
  the community firmware stack all of this runs on top of.

If you're one of the people above and want anything here changed
(attribution, licensing, or otherwise), please open an issue — this exists
to add to that work, not compete with it.

## Safety

Every command in `examples/` that moves a motor says so at the top of the
file, in a comment you'll see before it runs anything. Read it. Keep an eye
on the printer while any motor command is running, and don't leave it
unattended the first few times you try something new — a few of the commands
in this repo were only understood by watching what physically happened when
we sent them, sometimes not what we expected.

## Support

This is free, and the goal is to save the next person the hours we spent
here. If it saved you some and you'd like to say thanks:
[paypal.me/pakostatomas](https://paypal.me/pakostatomas) — entirely
optional, never expected.

## License

GPL-3.0 — see [`LICENSE`](LICENSE). Chosen to match the projects in
[Credits](#credits) this work builds on and cross-references.
