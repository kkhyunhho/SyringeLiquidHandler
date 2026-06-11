# LearnedPatterns — SyringeLiquidHandler

Running log of gotchas hit while building the combined SY-01B pump +
Entris-II balance scripts in this folder. Append a new entry whenever a
non-obvious problem is solved, using the **Problem / Cause / Fix / Rule**
format below. Newest entries at the bottom.

---

## 1. Bi-pass (M05) valve: firmware ports 1↔3 are the SAME fluid state

- **Problem**: With the dispense routine set to aspirate from valve port 3
  and dispense from port 1 (`SOURCE_PORT=3, DISPENSE_PORT=1`), the syringe
  drew and expelled at the **same** physical tube (the tip on port 1) and
  the reservoir was never drawn from — "nothing comes out." The valve and
  plunger moved correctly at the wire level (`/1I3R`, `/1A4800R`, `/1I1R`,
  `/1A0R` all sent; `?6` stepped 1→2→3 then back to 1), which masked the
  real issue for a long time.
- **Cause**: The bench valve is a Runze **M05 Bi-pass Flow Path** valve
  (the "MCC-4"), which has only **two** physical fluid states 90° apart:
  `C-1/2-3` (syringe↔physical port 1) and `C-3/1-2` (syringe↔physical
  port 3). The firmware is configured as a 4-way distribution valve, so it
  maps port digits to rotor angles 90° apart. Because the rotor pattern
  repeats every 180°, **firmware ports 1 and 3 land on the same fluid
  state** (and 2 and 4 on the other). Commanding 1↔3 is a 180° rotation
  that returns to the *same* connection — `?6` reports a different digit
  but the fluid path is identical. So aspirate@3 and dispense@1 were both
  the `C-1/2-3` (tip) state.
- **Fix**: Use a **90°-apart** port pair so the two commands hit different
  fluid states. Empirically confirmed on this bench: aspirate from
  **firmware port 2** (`C-3/1-2` → reservoir) and dispense from **firmware
  port 1** (`C-1/2-3` → tip). Firmware port 4 is interchangeable with 2,
  and firmware port 3 with 1.
- **Rule**: On a Bi-pass / dual-selection valve driven as a distribution
  valve, never assume `move_valve_to_port(n)` changing the `?6` digit means
  the fluid path changed. The two real states are 90° apart — pick source
  and sink ports that differ by 90° (e.g. 1 & 2), not 180° (1 & 3). Verify
  by watching which physical tube actually moves liquid, not by `?6`.

> Note: the SyringePumpController server's `/v1/prime` defaults
> (`source_port=3, sink_port=1`) have the **same** 180° bug; the ESP32
> path only works when the operator manually selects a 90°-apart pair.

---

## 2. Balance `read_stable_weight` returns too early after a dispense

- **Problem**: Reading the post-dispense mass with a single
  `read_stable_weight()` grabbed a value before the liquid had finished
  settling, so masses were recorded low/inconsistent and the script moved
  on without the weight truly confirmed.
- **Cause**: Under `COM.OUTP = AUTO W/` the balance auto-pushes a value on
  each stability event, and the *first* event after a dispense can fire
  early (droplet still spreading / line relaxing). Taking that first pushed
  value trusts the balance's loose, momentary stability call. A value
  buffered during the dispense could also be returned immediately.
- **Fix**: Added `read_settled_weight()` — it flushes the dispense
  transient, then waits until `SETTLE_AGREEMENT_READS` (default 3)
  consecutive stable readings agree within `SETTLE_TOLERANCE_G` (default
  0.001 g, ≈ the BCE224I's ~1 mg auto-push jitter) before accepting,
  bounded by a generous `SETTLE_TIMEOUT_S` (30 s). A per-read timeout means
  the pan went quiet → settled.
- **Rule**: Never trust a single auto-pushed "stable" reading for a value
  that changes right before the read. Require N consecutive in-tolerance
  readings (settling by agreement), and set the tolerance no tighter than
  the balance's own jitter or it will never converge.
