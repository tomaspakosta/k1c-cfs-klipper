# CFS RS-485 protocol — technical reference

Everything on this page was determined by combining:
1. Real captured traffic in [`ityshchenko/klipper-cfs`](https://github.com/ityshchenko/klipper-cfs)'s test suite (an `interceptty` dump from a real printer during multi-color printing),
2. Cross-referencing against two independent, more mature reimplementations — [`gitstonelabs/creality-cfs-klipper`](https://github.com/gitstonelabs/creality-cfs-klipper) and [`FrederickAlt/CREALITY-K1-AND-K1-MAX-CFS-RETRUDE-BEFORE-CUT-MOD`](https://github.com/FrederickAlt/CREALITY-K1-AND-K1-MAX-CFS-RETRUDE-BEFORE-CUT-MOD) — whose command tables use **different absolute command IDs** (different firmware/hardware variant) but validated our understanding of payload *shapes* and behavior,
3. Live empirical testing against a real CFS box, one command at a time, read-only first, motor commands last.

Where a value below is "empirically confirmed", we mean: we sent it to real hardware and watched what physically happened (motor movement, filament actually feeding, LED behavior), not just "the box replied without an error".

## Physical connection

The CFS box we tested connects over **USB, not a direct board-level UART** — there's a small USB-to-RS485 adapter inside the cable using a **CH340/CH341 chip** (`idVendor=1a86, idProduct=7523`). On Linux this just works: the kernel's `ch341-uart` driver (built into most distros, including stock K1/K1C firmware) picks it up automatically as `/dev/ttyUSB0`, no driver installation needed. Baud rate is **230400**, 8N1.

If your setup uses a different connection (e.g. a direct RS-485 header), the framing below is unaffected — only the transport changes.

## Frame format

```
head(1)=0xF7  slave_addr(1)  length(1)  status(1)  function_code(1)  data(N)  crc8(1)
```

- `length` = number of bytes *after* the length byte itself (`status + function_code + data + crc`)
- `crc8`: polynomial `0x07`, no init/xorout, computed over `[length, status, function_code, *data]` — i.e. everything between (and including) the length byte and the byte before the crc.
- Addressing: `0x01`-`0x04` for individual boxes once addressed, `0xFE` = broadcast to all boxes, `0xFF` = broadcast to everything on the bus.
- `status` in a *request* is normally `0xFF` for regular box functions, `0x00` for the auto-addressing command family (`0xA0`-`0xA3`).
- `status` in a *response* is the result code (see table below) — `0x00` = OK.

## Auto-addressing handshake

Boxes ship unaddressed and must be discovered + assigned an address each session (they forget it on power loss, but keep it across our script re-runs within a session):

```
1. Broadcast discovery:
   TX: f7 fe 05 00 a1 fe fe <crc>     (CMD_GET_SLAVE_INFO, data=[0xFE,0xFE])
   RX: f7 fe 11 00 a1 <14-byte UID> <crc>

2. Assign an address using that UID:
   TX: f7 fe 10 00 a0 <new_addr> <12-byte UID> <crc>   (CMD_SET_SLAVE_ADDR)
   RX: echoes UID back = accepted

3. (optional) confirm:
   TX: f7 <addr> 03 00 a2 <crc>        (CMD_ONLINE_CHECK)
   RX: echoes UID = box responds at that address now
```

## Command table (our validated numbering)

| Command | Function code | Notes |
|---|---:|---|
| `GET_RFID` | `0x02` | data=`[slot 0-3]`. Returns ASCII like `A:none;` when no RFID chip is present on the spool (most consumer/noname filament doesn't have one — this is expected, not an error). |
| `GET_REMAIN_LEN` | `0x03` | data=`[slot]` |
| `SET_BOX_MODE` | `0x04` | data=`[slot_or_zero, mode]`. `mode`: `0x00`=PRINT, `0x01`=IDLE. `slot_or_zero` is a single slot bitmask (`0x01`=A..`0x08`=D) to mark that specific slot as the active PRINT-mode slot once it's finished loading, or `0x00` for the generic "no particular slot" form used to bracket a sequence (what `data=[0x00,0x01]` — IDLE, no slot — actually means; this repo's `cfs_protocol.py` calls that `set_box_mode_idle()`). We originally thought the first byte was always a fixed `0x00` — it isn't. |
| `GET_BUFFER_STATE` | `0x05` | 1-byte response: `0x00`=middle, `0x01`=full, `0x02`=empty |
| `CTRL_CONNECTION_MOTOR_ACTION` | `0x07` | **The command that was missing from our early attempts.** data=`[action]`: `0x00`=STOP, `0x01`=EXTRUDE (mechanically connects the target slot to the shared feed path), `0x02`=RETRUDE. Call this *before* `EXTRUDE_PROCESS`. |
| `GET_FILAMENT_SENSOR_STATE` | `0x08` | data=`[bank]`. Bank `0x00` = MATERIAL: global 4-bit bitmask, which slots have filament present. Bank `0x01` = CONNECTIONS: which slot is currently mechanically connected. **Empirically confirmed bit mapping: bit0=A, bit1=B, bit2=C, bit3=D** (physical left-to-right), by pulling filament from one slot at a time and watching the bit clear. Independently cross-confirmed by `gitstonelabs/creality-cfs-klipper` — same function code, same `0x00`/`0x01` channel split, though they call it `GET_HARDWARE_STATUS` (a name collision with a *different* function in our own table below — don't confuse the two across projects). |
| `GET_BOX_STATE` | `0x0A` | General status. Response `status` byte can itself carry `UPDATE_STATE` (`0x30`) as an informational (not error) code, with 4 data bytes describing per-slot update events. |
| `SET_PRE_LOADING` | `0x0D` | data=`[slot_mask, phase]` — **not** `[slot_mask, enable]` as we first assumed, see below. |
| `MEASURING_WHEEL` | `0x0E`? | **Untested by us, position inferred not confirmed.** `gitstonelabs` documents this at `0x0E` with a `[GET\|CLEAN]` action byte, response = a 4-byte big-endian IEEE-754 float (mm, negative, magnitude grows as filament feeds). Their `SET_PRE_LOADING` also sits at `0x0D`, matching ours exactly, and `0x0E` is immediately next in sequence on both sides and is otherwise a gap in our own table — reasonable to guess it lines up, but we've never sent this ourselves. See the note under `EXTRUDE_PROCESS` below for why this might matter. |
| `TIGHTEN_UP_ENABLE` | `0x0F` | data=`[0x01]`/`[0x00]`. Wraps the `EXTRUDE_PROCESS` sequence. |
| `EXTRUDE_PROCESS` | `0x10` | data=`[slot, stage, amount]` — 3 bytes, `amount` is usually `0x00`. **We briefly "corrected" this to a 2-byte `[slot, stage]` form** after decompiling Creality's official firmware's host-side driver code, which appeared to send exactly that — it regressed live (even slot A started failing `PARAMS_ERR` immediately) and was reverted. This specific CFS unit's own onboard firmware apparently doesn't match whatever transport framing that host-side code assumes; trust live hardware behavior over source code when they disagree. `stage=0x07`'s real `amount` byte is unconfirmed - we use `0x02` (the decompiled source's special-cased value for that stage) as an untested-in-isolation guess. See the dedicated section below — this took the most work to get right, in two separate sessions. |
| `RETRUDE_PROCESS` | `0x11` | data=`[slot, stage]`, stages `0x00` then `0x01`. Reels filament back onto the spool. **Reliable on its own only for filament that hasn't been fed past the toolhead sensor.** For filament that reached the extruder's own drive gear (i.e. after a full `EXTRUDE`), this single call alone can leave it jammed there, needing a manual idler-lever release - see the "tip-forming unload" section below for the real fix. |
| `GET_VERSION_SN` | `0x14` | Returns an ASCII version/serial string. |
| `GET_HARDWARE_STATUS` | `0x15` | Returns what look like per-channel voltage/current readings. |

## Response state codes

| Code | Byte | Meaning |
|---|---:|---|
| `OK` | `0x00` | |
| `PARAMS_ERR` | `0x01` | |
| `CRC_ERR` | `0x02` | |
| `STATE_ERR` | `0x03` | Box isn't in a state that accepts this command right now. |
| `LENGTH_ERR` | `0x04` | |
| `EXTRUDE_ERR4` | `0x08` | |
| `EXTRUDE_ERR8` | `0x0C` | Seen repeatedly before we found the connection-motor step. |
| `EXTRUDE_ERR10` | `0x0D` | Same. |
| `RETRUDE_ERR2` | `0x13` | "Failed to exit connections" style error. |
| `RETRUDE_ERR7` | `0x1A` | |
| `UPDATE_STATE` | `0x30` | Informational, not an error — see `GET_BOX_STATE`. |

(This isn't the full list — only the codes we actually observed live. `MOTOR_LOAD_ERR`, `FILAMENT_ERR`, `SPEED_ERR`, `ENWIND_ERR` and others exist in the wider protocol but we never triggered them.)

## EXTRUDE_PROCESS — the whole story

This is the one that took real effort, so it's worth documenting the journey, not just the answer.

**What finally worked**, physically confirmed — first that filament pushed
past the internal buffer, and later (once the toolhead-side filament switch
was wired back up) confirmed reaching the toolhead sensor, seen by watching
Klipper's `filament_switch_sensor filament_sensor_2` flip from not-detected
to detected mid-run. **Caveat on that second result:** the PTFE tube to the
toolhead wasn't connected for this test, so the filament was feeding through
open air rather than a guided path, and it was manually guided into the
toolhead rather than arriving on its own. So this confirms the box can
extrude enough length and that the sensor read/logic works correctly — it
is *not* yet a confirmed fully automatic, hands-off box→PTFE→toolhead feed.
That's the next thing to verify once the tube is connected.

```
SET_BOX_MODE            data=[0x00, 0x01]              (IDLE mode, no slot)
CTRL_CONNECTION_MOTOR_ACTION  data=[0x01]               (ACTION=EXTRUDE — connects the feed path)
TIGHTEN_UP_ENABLE       data=[0x01]                     (enable)
EXTRUDE_PROCESS         data=[slot, 0x00, 0x00]         (stage 0)
EXTRUDE_PROCESS         data=[slot, 0x04, 0x00]         (stage 4)
EXTRUDE_PROCESS         data=[slot, 0x05, 0x00]         (stage 5 — poll this repeatedly;
                                                          this is where the motor actually runs.
                                                          The response's data bytes are live
                                                          telemetry and should visibly change
                                                          between polls if it's really moving.
                                                          Keep polling until the toolhead sensor
                                                          trips — ~10 polls got material past the
                                                          buffer, ~40 polls at a ~0.4s interval
                                                          reached the toolhead sensor.)
--- everything below this line was missing from every version of this repo
    before 2026-08-15/16 - we always stopped after stage 5 and went
    straight to cleanup. See "What we'd been missing entirely" below. ---
M83                                                     (toolhead: relative extrusion mode)
G0 E10 F35                                              (toolhead: slow prime move)
EXTRUDE_PROCESS         data=[slot, 0x06, 0x00]         (stage 6)
M83
G0 E5 F10                                               (toolhead: slower prime move)
EXTRUDE_PROCESS         data=[slot, 0x07, 0x02]         (stage 7 — the 3rd byte here is an
                                                          unconfirmed guess, see the command
                                                          table above)
SET_BOX_MODE            data=[slot, 0x00]               (mark this slot PRINT-mode loaded —
                                                          **this is the step that finally made
                                                          slot switching work**, confirmed live)
--- cleanup, same as before ---
TIGHTEN_UP_ENABLE       data=[0x00]                     (disable)
CTRL_CONNECTION_MOTOR_ACTION  data=[0x00]                (ACTION=STOP — cleanup)
SET_BOX_MODE            data=[0x00, 0x01]              (IDLE again — see note below)
```

**Post-run cleanup gotcha (found live, after remounting via the upgrade kit):** a completed `EXTRUDE_PROCESS` run can leave the box reporting a latched error status on `GET_BOX_STATE` — we saw `status=0x0C` (`EXTRUDE_ERR8`) persist across repeated queries, with the box's LED visibly red, **even though the extrude had actually succeeded** (toolhead sensor confirmed `filament_detected: true`). Simply sending `SET_BOX_MODE` (IDLE) again cleared it immediately (`status` back to `0x00`, LED back to white). Interoperability note: `gitstonelabs/creality-cfs-klipper`'s own `BOX_ERROR_CLEAR` command doesn't send anything to the box at all — it only clears their host-side cached error flag — so this "re-send `SET_BOX_MODE`" fix is something we found empirically, not something documented elsewhere. All the example scripts and the Klipper extra now send this as a final cleanup step after extrude.

**Confirmed while writing this doc, not just a lead:** the 4-byte "telemetry" data in stage-5 responses is a big-endian IEEE-754 float, exactly matching `MEASURING_WHEEL`'s documented format. Decoding our own captured sequence from the toolhead-reach test:

```
c5 34 53 4e -> -2885.207
c5 4d 69 2a -> -3286.573
c5 63 e7 b9 -> -3646.483
c5 7a 85 79 -> -4008.342
c5 88 98 b7 -> -4371.089
c5 93 dc c5 -> -4731.596
c5 a0 7a 66 -> -5135.300
c5 ac 11 6e -> -5506.179
c5 af a0 71 -> -5620.055   <- toolhead sensor triggers around here
c5 af 9f e3 -> -5619.986
c5 af 9f 89 -> -5619.942
```

All negative (matches the documented format exactly), magnitude climbing
smoothly while the motor was genuinely running, then flattening out to
essentially noise once the toolhead sensor confirmed material had arrived
— that's a clean, physically sane signal, not a coincidence. **Practical
implication:** a smarter `EXTRUDE_PROCESS` stage-5 loop could watch this
decoded value stabilize (stop changing beyond noise) as a real completion
signal, instead of the fixed poll-count loop `cfs_cli.py`/`examples/`
currently use. Not implemented yet — flagging it here as a concrete,
grounded next improvement rather than another guess.

**What didn't work, and why it's worth knowing:**

- Sending `EXTRUDE_PROCESS` alone (without `CTRL_CONNECTION_MOTOR_ACTION` first) fails deterministically with `EXTRUDE_ERR8` then `EXTRUDE_ERR10`, regardless of which slot you target in the `slot` byte. We tried this 4 different ways (different slot values, different sensor preconditions) before finding the missing step — all 4 produced byte-for-byte identical error responses, which in hindsight was itself a clue that the `slot` byte wasn't the variable that mattered.
- Even more confusingly: once we *did* get real motor movement (before adding the connection-motor step, purely by chance from other testing), the motor ran on a **different physical slot than the `slot` byte requested**. This makes sense in hindsight — without `CTRL_CONNECTION_MOTOR_ACTION`, the box just runs whatever slot's feed path happens to already be mechanically connected (a leftover/default state), completely ignoring the `slot` byte in `EXTRUDE_PROCESS` until the connection step actually routes it.
- **Correction, 2026-08-15 — the earlier version of this bullet was wrong.** We used to believe stopping the stage-5 polling loop and going straight to cleanup was "sufficient" and that `0x06` was just a recovery/retry state. It isn't — see "What we'd been missing entirely" below. Stages `0x06` and `0x07` (with real toolhead-side moves interleaved) are a required part of the sequence, not optional extras; skipping them may be exactly why this project could never get a slot other than A working reliably (see `TOOLCHANGE_TEST_PLAN.md` phase 2).
- The cutter turned out to be a non-issue for the extrude side: **it's not a CFS protocol command at all.** It's a pure toolhead-motion sequence — home X/Y, move to a configured pre-cut position, then to a configured cut position — implemented entirely as Klipper G-code macros, with no bytes sent to the CFS box. **Correction from an earlier version of this page:** we initially described the cut position as dragging filament across a stationary blade edge, based on external reference material for a different hardware revision. On our unit the mechanism is **lever-actuated** — hand-testing confirmed pressing a physical lever cleanly cuts the filament.

  Once the upgrade kit was properly mounted, we found the real `cut_pos` the safe way: jog the toolhead by hand via the printer's touchscreen (this keeps Klipper's homed position tracking intact — don't disable steppers and push the gantry by hand, you'll lose position reference and need to re-home) until the lever is visibly pressed, then read the exact coordinates back from Klipper (`toolhead`/`gcode_move` objects via Moonraker, or just the position display on the touchscreen). On our unit that's **`X=150.0, Y=225.0`** — notably different from `X=42.0` in an older reference config from a different physical printer, which is exactly why this needs measuring per-unit rather than assumed. We then confirmed it's fully reproducible with plain `G1 X150 Y225` moves — retreat (e.g. `G1 Y160`) then return reliably re-triggers the lever every time, no manual guidance needed once you have the real coordinates.

  **Gotcha:** if your printer happens to be in relative positioning mode (`G91`) when you try this, a `G1 Y160` retreat command will be interpreted as *relative* and can send the toolhead to `current_Y + 160`, which is likely out of range and will error. Send `G90` first to force absolute mode before jogging by coordinate.

  Don't assume any of the above for your own hardware without checking — hand-test the mechanism and measure the real position the same way we did. If you're chasing the full pipeline and don't have a cutter mounted/calibrated yet, that's fine: it doesn't block anything else documented on this page.
- `SET_PRE_LOADING` looked, from its one available real example and its name, like it should be "the" command to trigger feeding. In practice, sending it (with what we thought was a slot mask + enable byte) sometimes produced a brief, ambiguous state blip and sometimes nothing measurable at all — never a clear, repeatable "motor ran and moved material" result the way `EXTRUDE_PROCESS` (once fixed) and `RETRUDE_PROCESS` did.

  **Resolved, and corrected again since:** the second byte isn't an
  enable flag, it's an `action`. Our first correction (credit:
  `gitstonelabs/creality-cfs-klipper`, from the compiled stock firmware)
  named it "phase" with `ARM=0x00`/`DISARM=0x01`/`SLOT_REARM=0x02` -
  functionally on the right track, but with the **direction backwards**
  and **a 4th value neither of us had found**. Examining Creality's own
  official firmware directly (see `docs/TOOLCHANGE_TEST_PLAN.md` for how)
  gave the real, self-consistent picture:

  | Action | Byte | Meaning |
  |---|---:|---|
  | `CLOSE` | `0x00` | **Disable** pre-loading (the official code's own name/comment — the opposite of "ARM") |
  | `OPEN` | `0x01` | **Enable** pre-loading (opposite of "DISARM") |
  | `RUN` | `0x02` | Force-run the physical preload sequence for the given slot mask — a genuinely slow, blocking operation (timeout scales with how many slots are in the mask; matches the gitstonelabs' ~38s/slot timing note) |
  | `TIGHT` | `0x03` | Force-tighten — not documented anywhere we'd seen before this |

  So `0x00`/`0x01` really are a background toggle (just labeled
  backwards from our first guess), `0x02` is the one that actually
  moves anything, confirmed to be called with the full slot mask
  (`0x0F`) as a "reset to known state" step at the very start of the
  real official toolchange sequence (`CLOSE`, i.e. `[0x0F, 0x00]`) -
  worth doing the same before our own toolchange sequences. `0x03`
  (`TIGHT`) remains untested by us. Real captured reference frame from
  earlier in this project, `data=[0x0F, 0x01]`, now decodes as "OPEN
  (enable) all slots" - a plausible background-toggle call, not
  something that should have produced visible motor movement, which
  matches what we saw.

### What we'd been missing entirely — the real completion sequence

After 7 separate live failures trying to get any slot other than A
working, we ran out of things to reverse-engineer from wire traffic and
external projects alone, so we went straight to the source: downloaded
Creality's real official K1C firmware for our board variant from their
own download page and examined how its actual driver module sequences
`EXTRUDE_PROCESS`. (We're deliberately not detailing the extraction
method or reproducing any of that code here — see this project's private
research notes if you're doing the same investigation yourself; the
short version is that it's an ordinary Linux firmware image, no exotic
tooling required, and studying purchased hardware you own for
interoperability is the entire premise of this repo.)

The real sequence does **not** stop after stage 5 confirms the toolhead
sensor. It continues with a slow toolhead-side prime move, stage 6, an
even slower prime move, then stage 7 — shown in the sequence diagram
above. Only after stage 7 completes does it mark the slot as the active
"loaded" one via `SET_BOX_MODE`'s per-slot form. We had never sent stage
6, stage 7, or moved the toolhead extruder as part of `EXTRUDE_PROCESS`
at all — we always stopped right after stage 5 and jumped to cleanup.

This plausibly explains multiple things we'd puzzled over separately:
why a completed extrude could leave a latched error status (we never
told the box the load actually finished, the "proper" way); and why
trying to switch to a different slot afterward always failed the same
way regardless of what we varied (the box may never have exited its
"mid-load" state for the first slot in the first place).

**Confirmed live, 2026-08-16.** After also finding and reverting a
regression in `EXTRUDE_PROCESS`'s payload byte count (see the command
table above), running this full sequence against slot D — never
successfully touched before, across two separate sessions — actually
worked: stages 6 and 7 both returned clean `OK`, `filament_detected`
went true, and `GET_FILAMENT_SENSOR_STATE`'s CONNECTIONS bank reported
`0x08` (slot D), the first time all project long the box's own
"connected slot" state reflected anything other than A. See
`TOOLCHANGE_TEST_PLAN.md` phase 2 for the full trail.

## RETRUDE — the tip-forming unload sequence

A plain `RETRUDE_PROCESS` call (box-side only) works fine for filament
that never made it past the toolhead sensor. But once `EXTRUDE` has
pushed material all the way into the toolhead extruder's own drive
gear - which the completed sequence above does on purpose, to prime it
- a single `RETRUDE_PROCESS` call can leave that filament jammed there.
Confirmed live, repeatedly: the box reports `RETRUDE_ERR2`/`RETRUDE_ERR7`
("failed to exit connections"), the toolhead sensor never clears no
matter how long you wait, and the only fix in the moment was manually
releasing the toolhead extruder's idler lever.

The real cause, and the fix, turned up the same way the stage 6/7
completion did - by examining Creality's own official firmware. It
turns out unloading isn't just "call `RETRUDE_PROCESS` and wait" there
either; it's preceded by a **tip-forming sequence**: a deliberate
pattern of small toolhead-side extrude/retract moves that re-melts and
re-shapes the filament tip into a smooth taper before the real pull
starts. This is the same idea Bambu's AMS and Prusa's MMU use - a
blobby or snagged tip (whatever shape it happened to cool into) is what
catches in a drive gear on the way out; a clean taper doesn't.

The move table (12 steps, `(distance_mm, speed_mm_per_min)`, positive =
extrude):

```
(0.5, 600), (-5, 600), (2.5, 600), (-1.25, 600), (1.75, 600), (1, 60),
(-15, 90), (-15, 90), (-15, 500), (-15, 500), (-15, 500), (-15, 500)
```

The first 6 steps net only about **-0.5mm** of real movement - that's
the wiggle. Its last step is deliberately very slow (60mm/min for just
1mm) to give the re-melted tip time to actually solidify into shape
before anything real happens. Only then do the last 6 steps do the
**real retraction: -15mm each, -90mm total** - far more than the
-15mm-once we originally tried, which is very likely why that didn't
reliably work either.

The other piece: each of those -15mm steps is preceded by a **generic
`RETRUDE_PROCESS` call with `data=[0x00, 0x00]`** - slot byte `0x00`
(no specific slot, not the actual A-D bitmask), trigger `0x00`
(BUFFER) - checked in with the box before every chunk of toolhead
movement, not just once at the start. If that call's response
indicates the box thinks it's done, the sequence checks the *toolhead
sensor* to confirm, and stops early the moment it reads clear rather
than always running the full -90mm.

An implementation of this (own re-implementation of the technique, not
a copy of anyone's source - see the private research log's provenance
note) is in `cfs_protocol.py`'s `TIP_FORM_STEPS` plus
`retrude_with_tip_form()` in `cfs_cli.py`, and built into
`klipper_extra/creality_cfs.py`'s `CFS_RETRUDE`. **Not yet tested live**
as of this writing - see `TOOLCHANGE_TEST_PLAN.md` for status.

## Firmware background (if you want to go deeper)

The CFS box's own MCU runs **RT-Thread** (a Chinese-origin RTOS) on what's electrically a **STM32F103VET6**-compatible chip (bought as GD32F303VET6 in the Creality-branded kits). It has a separate "hub" motor (distinct from the four per-slot feeder motors) that's very likely what `CTRL_CONNECTION_MOTOR_ACTION` actually drives — physically routing one feeder's filament path into the shared extrude channel. None of this is from official documentation; it comes from firmware string analysis in [`fake-name/cfs-reverse-engineering`](https://github.com/fake-name/cfs-reverse-engineering), which also has PCB photos, a partial Ghidra project, and raw firmware/RAM dumps if you want to dig further yourself.
