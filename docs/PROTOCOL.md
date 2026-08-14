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
| `SET_BOX_MODE` | `0x04` | data=`[0x00,0x01]` = IDLE mode (needed before manual/scripted control — disables the box's own automatic buffer management) |
| `GET_BUFFER_STATE` | `0x05` | 1-byte response: `0x00`=middle, `0x01`=full, `0x02`=empty |
| `CTRL_CONNECTION_MOTOR_ACTION` | `0x07` | **The command that was missing from our early attempts.** data=`[action]`: `0x00`=STOP, `0x01`=EXTRUDE (mechanically connects the target slot to the shared feed path), `0x02`=RETRUDE. Call this *before* `EXTRUDE_PROCESS`. |
| `GET_FILAMENT_SENSOR_STATE` | `0x08` | data=`[bank]`. Bank `0x00` = MATERIAL: global 4-bit bitmask, which slots have filament present. Bank `0x01` = CONNECTIONS: which slot is currently mechanically connected. **Empirically confirmed bit mapping: bit0=A, bit1=B, bit2=C, bit3=D** (physical left-to-right), by pulling filament from one slot at a time and watching the bit clear. |
| `GET_BOX_STATE` | `0x0A` | General status. Response `status` byte can itself carry `UPDATE_STATE` (`0x30`) as an informational (not error) code, with 4 data bytes describing per-slot update events. |
| `SET_PRE_LOADING` | `0x0D` | data=`[slot_mask, enable]`. This looked like the "run the feeder motor" command at first, but turned out to behave more like a background monitoring toggle — see "Dead ends" below. |
| `TIGHTEN_UP_ENABLE` | `0x0F` | data=`[0x01]`/`[0x00]`. Wraps the `EXTRUDE_PROCESS` sequence. |
| `EXTRUDE_PROCESS` | `0x10` | data=`[slot, stage, amount]`. See the dedicated section below — this took the most work to get right. |
| `RETRUDE_PROCESS` | `0x11` | data=`[slot, stage]`, stages `0x00` then `0x01`. **Fully working, physically confirmed** — reels filament back onto the spool. Much simpler than extrude; no connection-motor precursor needed. |
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
SET_BOX_MODE            data=[0x00, 0x01]              (IDLE mode)
CTRL_CONNECTION_MOTOR_ACTION  data=[0x01]               (ACTION=EXTRUDE — connects the feed path)
TIGHTEN_UP_ENABLE       data=[0x01]                     (enable)
EXTRUDE_PROCESS         data=[slot, 0x00, 0x00]         (stage 0)
EXTRUDE_PROCESS         data=[slot, 0x04, 0x00]         (stage 4)
EXTRUDE_PROCESS         data=[slot, 0x05, 0x00]         (stage 5 — poll this repeatedly;
                                                          this is where the motor actually runs.
                                                          The response's data bytes are live
                                                          telemetry and should visibly change
                                                          between polls if it's really moving.
                                                          Keep polling: ~10 polls got material
                                                          past the buffer, but it took ~40 polls
                                                          at a ~0.4s interval for it to reach the
                                                          toolhead sensor - there's no separate
                                                          "go further" command, just keep going.)
TIGHTEN_UP_ENABLE       data=[0x00]                     (disable)
CTRL_CONNECTION_MOTOR_ACTION  data=[0x00]                (ACTION=STOP — cleanup)
```

**What didn't work, and why it's worth knowing:**

- Sending `EXTRUDE_PROCESS` alone (without `CTRL_CONNECTION_MOTOR_ACTION` first) fails deterministically with `EXTRUDE_ERR8` then `EXTRUDE_ERR10`, regardless of which slot you target in the `slot` byte. We tried this 4 different ways (different slot values, different sensor preconditions) before finding the missing step — all 4 produced byte-for-byte identical error responses, which in hindsight was itself a clue that the `slot` byte wasn't the variable that mattered.
- Even more confusingly: once we *did* get real motor movement (before adding the connection-motor step, purely by chance from other testing), the motor ran on a **different physical slot than the `slot` byte requested**. This makes sense in hindsight — without `CTRL_CONNECTION_MOTOR_ACTION`, the box just runs whatever slot's feed path happens to already be mechanically connected (a leftover/default state), completely ignoring the `slot` byte in `EXTRUDE_PROCESS` until the connection step actually routes it.
- A stage numbered `0x06` appears in one real captured reference dump (from someone else's box, different firmware revision), so we initially tried `...→ 0x05 → 0x06`. Other reference material describes the normal sequence as `0 → 4 → 5 → 7` instead, with `0x06` being a recovery/retry state rather than a normal step. We never needed to explicitly send `0x07` either — stopping the stage-5 polling loop and moving to cleanup was sufficient once the connection-motor step was in place. Your mileage may vary by firmware revision; if stage 5 polling errors out for you even with the connection-motor step done, trying an explicit `0x07` afterward is a reasonable next thing to check.
- Two independent external references describe stages `0x06`/`0x07` (in *their* numbering) as gated by "the toolhead filament switch" activating. We initially had that sensor disconnected and could still get material past the buffer without it — but once we wired the sensor back up and simply kept polling stage 5 for longer (~40 polls instead of ~10), the filament reached the toolhead sensor on its own, with no explicit `0x06`/`0x07` ever sent. So for this firmware revision at least, stage 5 alone — given enough time — drives the whole feed; the "gating" in those other references may be about the *wrapper* deciding when to stop polling and declare success, not about the box firmware requiring a distinct stage transition. (See the caveat above though — that result was without a PTFE tube connected, so treat it as "the motor/sensor path works," not yet as "a fully automated feed.")
- The cutter turned out to be a non-issue for the extrude side: **it's not a CFS protocol command at all.** It's a pure toolhead-motion sequence — home X/Y, move to a configured pre-cut position, then to a configured cut position — implemented entirely as Klipper G-code macros, with no bytes sent to the CFS box. **Correction from an earlier version of this page:** we initially described the cut position as dragging filament across a stationary blade edge, based on external reference material for a different hardware revision. On our unit the mechanism is **lever-actuated** — hand-testing confirmed pressing a physical lever cleanly cuts the filament.

  Once the upgrade kit was properly mounted, we found the real `cut_pos` the safe way: jog the toolhead by hand via the printer's touchscreen (this keeps Klipper's homed position tracking intact — don't disable steppers and push the gantry by hand, you'll lose position reference and need to re-home) until the lever is visibly pressed, then read the exact coordinates back from Klipper (`toolhead`/`gcode_move` objects via Moonraker, or just the position display on the touchscreen). On our unit that's **`X=150.0, Y=225.0`** — notably different from `X=42.0` in an older reference config from a different physical printer, which is exactly why this needs measuring per-unit rather than assumed. We then confirmed it's fully reproducible with plain `G1 X150 Y225` moves — retreat (e.g. `G1 Y160`) then return reliably re-triggers the lever every time, no manual guidance needed once you have the real coordinates.

  **Gotcha:** if your printer happens to be in relative positioning mode (`G91`) when you try this, a `G1 Y160` retreat command will be interpreted as *relative* and can send the toolhead to `current_Y + 160`, which is likely out of range and will error. Send `G90` first to force absolute mode before jogging by coordinate.

  Don't assume any of the above for your own hardware without checking — hand-test the mechanism and measure the real position the same way we did. If you're chasing the full pipeline and don't have a cutter mounted/calibrated yet, that's fine: it doesn't block anything else documented on this page.
- `SET_PRE_LOADING` looked, from its one available real example and its name, like it should be "the" command to trigger feeding. In practice, sending it (with a plausible slot mask + enable byte) sometimes produced a brief, ambiguous state blip and sometimes nothing measurable at all — never a clear, repeatable "motor ran and moved material" result the way `EXTRUDE_PROCESS` (once fixed) and `RETRUDE_PROCESS` did. We suspect it's closer to a background-monitoring/auto-preload toggle (matching debug strings found in the box's own firmware: `preloading enable/disable/aging/read`) than a direct "run motor now" trigger. Worth knowing before you spend time on it expecting motor movement.

## Firmware background (if you want to go deeper)

The CFS box's own MCU runs **RT-Thread** (a Chinese-origin RTOS) on what's electrically a **STM32F103VET6**-compatible chip (bought as GD32F303VET6 in the Creality-branded kits). It has a separate "hub" motor (distinct from the four per-slot feeder motors) that's very likely what `CTRL_CONNECTION_MOTOR_ACTION` actually drives — physically routing one feeder's filament path into the shared extrude channel. None of this is from official documentation; it comes from firmware string analysis in [`fake-name/cfs-reverse-engineering`](https://github.com/fake-name/cfs-reverse-engineering), which also has PCB photos, a partial Ghidra project, and raw firmware/RAM dumps if you want to dig further yourself.
