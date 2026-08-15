# Tool-change (T0-T3) test plan — staged, supervised

Goal: get from "individual validated pieces" (RETRUDE, EXTRUDE, cut
position) to a working material-swap sequence a slicer could eventually
drive via `T0`-`T3`. Every step here is meant to be done **at the printer,
watching it**, one at a time, in order. Don't skip ahead — each step
either confirms a piece we haven't tested yet, or confirms the pieces
still work together after the upgrade-kit remount.

Reference sequence (from FrederickAlt's `material-change-flow.md`, a
different hardware revision, but the *order* of operations is the useful
part here):

```
cut current filament (if any)
retrude old material
load target material
printer-extruder-side extrusion
flush (purge old->new)
```

## Phase 1 — re-confirm the individual pieces ✅ DONE (2026-08-14, at the printer, post upgrade-kit remount)

Each of these is a single command, watch the printer each time.

1. **`python cfs_cli.py status`** — read-only, confirms the box responds
   and reports which slots have material. ✅ box responded, UID/version
   matched.
2. **`python cfs_cli.py retrude --slot <X>`** — pick whichever slot
   currently has filament loaded. Confirms RETRUDE still works after the
   remount. ✅ motor reeled in as expected.
3. **One single cut pass**, *not* the full multi-pass macro yet — send
   just the two moves by hand to confirm the lever still triggers at the
   coordinates found earlier:
   ```
   G90
   G1 X150 Y225 F1500
   ```
   (adjust to your own measured coordinates if different from ours).
   Confirm the lever presses, then retreat: `G1 Y200 F1500`. ✅ lever
   pressed/released reliably, reproducible with plain G-code.
4. **`python cfs_cli.py extrude --slot <X>`** — same slot as step 2 or a
   fresh one, confirms EXTRUDE still reaches the toolhead sensor after the
   remount. Watch `filament_detected` in Fluidd/Moonraker if you have the
   Klipper extra installed, or poll it manually. ✅ material physically
   reached the toolhead sensor, `filament_detected: true`, fully
   automatic (no manual guidance needed this time, PTFE run was fully
   connected).

All 4 passed — the individual pieces are confirmed working on the
remounted hardware.

**Two real incidents hit during this run, both now understood and fixed:**

- **Box went completely silent** (no reply even to read-only
  `GET_BOX_STATE`) partway through. Cause: filament was physically loose
  in slot A's feed gear — the sensor still reported "loaded" but there
  was no real grip, so the feed mechanism jammed/faulted hard enough to
  stop the box responding at all. Fix: physically re-seat the filament
  so the feed gear has real resistance against it, then re-run discovery
  (`discover()` + `assign_address()`) to restore comms — a plain retry
  without physically fixing the filament will not help.
- **After a successful EXTRUDE, `GET_BOX_STATE` stayed on `status=0x0C`
  (`EXTRUDE_ERR8`), LED visibly red**, even though the extrude had
  actually worked (filament confirmed at the toolhead sensor). This is a
  latched status flag, not a real fault. Fix: resend `SET_BOX_MODE`
  (IDLE) — clears it immediately. This is now done automatically at the
  end of every extrude call in this repo (`cfs_cli.py`,
  `examples/03_extrude.py`, `klipper_extra/creality_cfs.py`) — see
  docs/PROTOCOL.md's "Post-run cleanup gotcha" for the full writeup.

Move to phase 2.

## Phase 2 — manual end-to-end swap ✅ RESOLVED (2026-08-16) — see the update near the bottom of this section for how

*(This section kept its whole debugging trail below on purpose - it took
two long sessions and a real regression scare to get here, and the trail
is useful if you ever hit something similar. Skip to "RESOLVED, for
real" near the bottom if you just want the answer.)*

This is the real test: does cut -> retrude -> extrude work **as a
sequence**, not just individually.

1. Make sure slot A (or whichever slot is currently loaded at the
   toolhead) has filament reaching the toolhead sensor (run
   `extrude --slot A` from phase 1 if not). ✅ done (carried over from
   phase 1).
2. Run the single cut pass (phase 1, step 3) to sever it. ✅ done.
3. `python cfs_cli.py retrude --slot A` — pull the now-severed material
   back into the box. Watch that it retracts cleanly (no snag at the cut
   point). ✅ done, retracted cleanly.
4. `python cfs_cli.py extrude --slot B` (a *different* slot) — load the
   new material forward to the toolhead sensor. ❌ **repeatedly failed,
   3 attempts, different symptoms each time** (latched error status that
   wouldn't clear / total box silence / motor spun on the wrong slot
   while the requested slot errored). Root cause, high confidence:
   **the box never actually switches which slot is "connected" to the
   shared feed path** — it stayed mechanically connected to slot A (the
   first slot we ever used) the whole time, regardless of which slot
   we asked `EXTRUDE_PROCESS` to use. The real slot-switch mechanism is
   still unknown. Full write-up, all 3 attempts, and the diagnostic
   test that proved it (`EXTRUDE --slot C` produced real motion at slot
   A while C's LED errored) are in the private research log.
5. Confirm `filament_detected` is true again, and that what's physically
   at the toolhead is now slot B's filament, not leftover A. — not
   reached.

**Leading hypothesis for next session:** `CFS_RETRUDE`'s box-side
sequence alone may be incomplete — a reference implementation
(gitstonelabs/creality-cfs-klipper) pairs its unload with an actual
**toolhead-side retraction** (`G1 E-15 F360` on the printer's own
extruder, mid-sequence, hotend heated) and gates completion on the
toolhead filament switch actually clearing, which our `retrude_stage()`
never does. A first live test of this (same evening, hotend heated to
220°C) didn't give a clean answer either way — it hit a real (if
practically harmless) `FR2832` "feeding/retraction jam" fault, most
likely because slot A had already been retruded down to a short leftover
stub earlier that same session, not enough material for the sequence's
long reel-in phase. **Retest on a slot that hasn't been touched yet
this session** (full spool-side material, never extruded/retruded)
before drawing a real conclusion.

**Update, next session: 7 live attempts (B×3, C×1, D×3, including on a
freshly-booted box that had never touched slot A) all failed the same
way — this isn't a session-history or payload-length issue, slot A
currently just seems to be the only one that works.** The most promising
untested lead now: a real factory `box.cfg` found on the same board
variant (`CR4CU220812S12`) shows the *official* Creality load sequence
includes `BOX_GO_TO_EXTRUDE_POS` (moving the toolhead to a specific
position, `X148/Y225.3/Z30` by factory default) and an error-clear step
*before* starting — both of which our own testing never included.
Notably, that other printer's official `box.py` **did work on all 4
slots at first** using the full sequence, before developing an
unexplained slot-A-only regression in a later session (see the CFS
project's memory / `FINDINGS.md` for the full story) — so this isn't
guaranteed to fix it, but it's the one real piece of the official
sequence we haven't tried. Added (untested) to
`klipper_extra/creality_cfs.py`'s `CFS_EXTRUDE` — see that file's
`cmd_CFS_EXTRUDE` for details before testing it live. Full 2-byte
`CTRL_CONNECTION_MOTOR_ACTION` payload test, a claimed-but-unverified
"GPIO slot selector" theory (checked against the reference source —
not real, that GPIO is for the shared buffer's fill sensor, unrelated),
and a `gitstonelabs` mock-suite dead-end are all written up in
`FINDINGS.md` for anyone who wants the full trail before re-testing.

**If something snags or doesn't retract cleanly at step 3**: stop and
look at it physically before continuing - a botched retrude right after a
cut is exactly the kind of thing that can jam a Bowden path. Don't push
through it by re-running extrude on top of a snag.

**LED error reference, useful going forward** (from Creality's CFS
error-code wiki): all slots flashing red together = communication error
(`FS2831`, printer can't talk to the box at all); **double red flash on
one specific slot = feeding/retraction jam (`FR2832`)** — exactly what
we hit; solid/stuck sensor status = debris or a jammed micro-switch in
the feed path.

**✅ RESOLVED, for real (2026-08-16, next session, at the printer).**
Two things, found together:

1. **A regression, found and fixed first.** A 2-byte `EXTRUDE_PROCESS`
   payload (`[slot, stage]`) looked correct based on examining Creality's
   real official firmware, but broke live - even slot A, reliable for
   sessions, started failing `PARAMS_ERR` immediately. Reverted to the
   original 3-byte form (`[slot, stage, amount]`, `amount` usually
   `0x00`) and slot A worked again instantly. Lesson: this specific CFS
   unit's own onboard firmware doesn't necessarily match whatever
   transport framing the *decompiled printer-side driver* code assumes -
   trust live hardware behavior over source code when they disagree.
2. **The actual fix: completing the full `EXTRUDE_PROCESS` sequence.**
   With the payload regression fixed, `EXTRUDE --slot D` (never touched
   successfully before, in either session) actually moved slot D's own
   motor - a first. Stages 6 and 7 (with the toolhead `G0 E10 F35` /
   `G0 E5 F10` prime moves between them, see `docs/PROTOCOL.md`) both
   returned clean `OK`, `filament_detected` went true, and critically
   `GET_FILAMENT_SENSOR_STATE`'s CONNECTIONS bank finally reported
   `0x08` (slot D) instead of `0x01` (slot A) - the first time all
   project long that the box's own "connected slot" state reflected
   anything other than A. **Our previous EXTRUDE "successes" were only
   ever the first half of the real sequence - enough to reach the
   toolhead sensor, but never enough to make the box consider the load
   properly finished, which is apparently a precondition for switching
   slots at all.**

**Confirmed on a second and third slot the same session: `EXTRUDE
--slot B` and `EXTRUDE --slot C` both worked too** — connection
bitmask read back `0x02` (B) and `0x04` (C) respectively afterward,
`GET_BOX_STATE` clean (`0x00`) both times, `filament_detected` true
both times. **All 4 slots (A, B, C, D) are now confirmed able to
switch cleanly with the completed `EXTRUDE_PROCESS` sequence** — this
is not a fluke specific to any one slot.

For B, material physically reached past the toolhead sensor into the
extruder gear itself (a brief red LED blip during that handoff cleared
on its own to white within seconds - not a real fault, just a normal
part of the load). Because it went in that deep, the box-side motor
alone couldn't pull it back out on retrude - **manually releasing the
toolhead extruder's idler lever was needed before it would retract
cleanly into the box.**

**Fixed for C: a small toolhead-side retraction before cutting.** At
the cut position, before retreating: `M83` then `G1 E-15 F300` (pull
the tip back out of the extruder's own grip), *then* retreat and run
`RETRUDE` as normal. Tested live on slot C - retracted "krásně čistě"
(cleanly), no manual lever assist needed. **Worth building into
`CFS_TOOLCHANGE`/a future unload macro as a standard step**, not just
an emergency fix.

**Important operational lessons learned live this session:**
1. Always CUT before RETRUDE if there's filament past the cutter
   (toward the toolhead) — retruding without cutting first can only
   pull back the box-side portion and left a stray strand stuck at the
   toolhead once, which then snapped when retruded anyway. Cut →
   (toolhead pre-retract, see below) → retrude fully into the box →
   only then select a new slot and extrude.
2. Add a toolhead-side pre-retract (`M83` + `G1 E-15 F300`) right
   after cutting, before the box-side retrude - avoids needing a manual
   extruder-lever assist for filament that fed in deep.
3. If the box stops responding to RS-485 at all and a normal
   power-cycle doesn't fix it, check the box's own physical
   temperature/humidity display - if that's blank too, the box itself
   isn't properly booted, not just disconnected from the bus. A longer,
   more deliberate power-cycle (wait for that display to come back)
   resolved it.

**Open question, not yet resolved:** after the slot-C retrude above,
the box stopped responding to RS-485 again (same silent pattern as
lesson 3), and `filament_switch_sensor filament_sensor_2` still read
`filament_detected: true` right afterward despite the retrude feeling
clean - unclear if that's a stale/pre-update sensor read or filament
genuinely still sitting at the sensor. Not yet physically confirmed
either way - check next session.

`BOX_NOZZLE_CLEAN` plus stage 7's exact 3rd payload byte remain
unconfirmed guesses - neither blocked any of these results. Move to
phase 3.

## Phase 3 — turn it into a macro

Only after phase 2 has worked cleanly at least once by hand:

1. Add a `[save_variables]` section to `printer.cfg` if you don't already
   have one (see `macros/toolchange_draft.cfg`'s header comment for the
   exact snippet) — `CFS_TOOLCHANGE` uses it to remember which slot is
   active across calls, so you don't have to pass `FROM=` by hand every
   time.
2. Load `macros/toolchange_draft.cfg` — **written from the phase 2 steps
   above, not yet run as a single macro**, same "draft" status as
   `cut_macro_draft.cfg`.
3. Test *that macro* the same way — supervised, one slot swap, watching
   every step: `CFS_TOOLCHANGE FROM=A TO=B` explicitly the first time,
   then `CFS_TOOLCHANGE TO=A` (no `FROM`) to confirm the saved-state
   lookup picked up B correctly from the previous call.

## Phase 4 — flush and T0-T3 (further out, more speculative)

Only after phase 3's swap-only macro is confirmed working repeatedly:

1. `macros/flush_draft.cfg`'s `CFS_FLUSH` is the **least validated piece
   in this repo** — there's no empirical data behind it at all beyond "a
   printer-side purge extrusion belongs somewhere around here" from the
   reference docs. Test it completely standalone first (heated, away from
   any model, watching it) before ever chaining it after a toolchange.
2. Once both `CFS_TOOLCHANGE` and `CFS_FLUSH` are independently trusted,
   `macros/tool_aliases_draft.cfg` wires `T0`-`T3` to call them together
   using the saved active-slot state. Call `T0`/`T1`/etc. by hand from the
   console and confirm the result before ever pointing a slicer at them.
3. Only after *that* works reliably by hand does it make sense to touch
   OrcaSlicer settings at all — see the notes below.

## Setting up your slicer (for later — not relevant until `T0`-`T3` exist and work)

Not attempted or tested in this project yet — only including what's known
from prior work on *different* CFS hardware (an official-firmware setup,
not this repo's custom protocol client), as a starting point once you
actually reach this stage:

- OrcaSlicer's `manual_filament_change` option needs to be `0`/off —
  otherwise Orca inserts a `PAUSE` at every color change instead of
  sending an automatic `Tn` tool-change command, and nothing here would
  ever get called.
- Empirically, Orca's filament slots map to `T0`-`T3` sequentially by
  slot position (Orca slot 1 → `T0`, slot 2 → `T1`, etc.), which in turn
  would map to physical box slots A-D in the same order — consistent with
  the bit-mapping (`bit0=A`..`bit3=D`) validated elsewhere in this repo.
- `purge_in_prime_tower`/`enable_prime_tower` and `wiping_volumes_extruders`
  are the settings that controlled purge behavior on that other setup —
  likely relevant again once this repo's own flush/purge step exists, but
  unverified here.

None of this can be usefully tested until `CFS_TOOLCHANGE` (or `T0`-`T3`
aliased to it) actually works end to end per the phases above.

## What's deliberately not covered yet

- Purge/flush logic (pushing enough material through to clear the old
  color) — phase 2/3 above only prove the swap mechanics, not print
  quality. That's a separate tuning pass once swapping itself is solid.
- Multiple cut passes / retry logic from the reference docs — start with
  one clean pass; only add retries if a single pass proves unreliable.
- Any OrcaSlicer profile changes — not worth touching until `T0`-`T3`
  actually exist and work standalone from the console first.
