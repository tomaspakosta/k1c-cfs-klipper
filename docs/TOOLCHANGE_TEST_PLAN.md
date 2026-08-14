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

## Phase 1 — re-confirm the individual pieces (all already validated once, before the upgrade kit remount)

Each of these is a single command, watch the printer each time.

1. **`python cfs_cli.py status`** — read-only, confirms the box responds
   and reports which slots have material.
2. **`python cfs_cli.py retrude --slot <X>`** — pick whichever slot
   currently has filament loaded. Confirms RETRUDE still works after the
   remount.
3. **One single cut pass**, *not* the full multi-pass macro yet — send
   just the two moves by hand to confirm the lever still triggers at the
   coordinates found earlier:
   ```
   G90
   G1 X150 Y225 F1500
   ```
   (adjust to your own measured coordinates if different from ours).
   Confirm the lever presses, then retreat: `G1 Y200 F1500`.
4. **`python cfs_cli.py extrude --slot <X>`** — same slot as step 2 or a
   fresh one, confirms EXTRUDE still reaches the toolhead sensor after the
   remount. Watch `filament_detected` in Fluidd/Moonraker if you have the
   Klipper extra installed, or poll it manually.

If all 4 pass, the individual pieces are confirmed working on the
remounted hardware — move to phase 2.

## Phase 2 — manual end-to-end swap (no macro yet, just the sequence by hand)

This is the real test: does cut -> retrude -> extrude work **as a
sequence**, not just individually.

1. Make sure slot A (or whichever slot is currently loaded at the
   toolhead) has filament reaching the toolhead sensor (run
   `extrude --slot A` from phase 1 if not).
2. Run the single cut pass (phase 1, step 3) to sever it.
3. `python cfs_cli.py retrude --slot A` — pull the now-severed material
   back into the box. Watch that it retracts cleanly (no snag at the cut
   point).
4. `python cfs_cli.py extrude --slot B` (a *different* slot) — load the
   new material forward to the toolhead sensor.
5. Confirm `filament_detected` is true again, and that what's physically
   at the toolhead is now slot B's filament, not leftover A.

If this works cleanly, you have a manually-proven material swap. That's
the point to write it up as a real macro (phase 3) rather than before.

**If something snags or doesn't retract cleanly at step 3**: stop and
look at it physically before continuing - a botched retrude right after a
cut is exactly the kind of thing that can jam a Bowden path. Don't push
through it by re-running extrude on top of a snag.

## Phase 3 — turn it into a macro

Only after phase 2 has worked cleanly at least once by hand:

1. Draft a `CFS_TOOLCHANGE` macro combining the phase 2 steps (see
   `macros/toolchange_draft.cfg` in this repo — **written from the phase 2
   steps above, not yet run as a single macro**, same "draft" status as
   `cut_macro_draft.cfg`).
2. Test *that macro* the same way — supervised, one slot swap, watching
   every step.
3. Only after the macro itself is confirmed working, consider wiring
   `T0`-`T3` gcode command aliases to it for actual slicer use — that's
   further out and not attempted yet.

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
