# SyringeLiquidHandler

Cross-project bench scripts that drive the **Runze SY-01B syringe pump**
and the **Sartorius Entris-II balance** together for liquid-handling
measurements. The pump (`sy01b`) and balance (`entris_ii`) drivers are
`pip install -e`'d into the shared conda env **`sdl`**, so
[`cv_mass_measurement.py`](cv_mass_measurement.py) imports them directly —
no `sys.path` bootstrap.

The optional XZ stage ([`xz_stage.py`](xz_stage.py)) uses the
MKSServo57DCANController **standalone** MKS driver, which is not in `sdl`
(its `mks_motor` import name collides with the full ESP32 driver); that one
is added to `sys.path` from `../MKSServo57DCANController/src`.

## Scripts

| File | Purpose |
|---|---|
| `cv_mass_measurement.py` | Gravimetric accuracy + precision (CV) of pump dispense volumes (5/50/100 µL × 5 reps), written to a timestamped `.xlsx`. Optionally homes + positions the XZ frame first. |
| `xz_stage.py` | Brings the XZ gantry (MKS SERVO57D motors) up: home + move to the measurement position. Used by `cv_mass_measurement.py`; also runnable standalone. |

## `cv_mass_measurement.py`

Measures how repeatably the pump dispenses small water volumes, weighing
each dispense on the balance:

0. If `MOTOR_STAGE_ENABLE`, home the XZ gantry and move it to the
   measurement position (`xz_stage.home_and_position()`) — see below.
1. Place **one empty vial** on the pan (once, at the prompt).
2. The pump primes (`PRIME_CYCLES` full-stroke cycles) to fill the line and
   purge air, then `MEASUREMENT_PLAN` drives two nested loops — the **outer**
   loop over each `(target volume, replicates)` entry, the **inner** loop
   over that volume's replicates. Each inner iteration:
   - tares the balance and confirms the zero baseline,
   - dispenses the volume — aspirate from `SOURCE_PORT` (2), dispense
     through `DISPENSE_PORT` (1) — see the valve note below,
   - waits for the weight to settle (consecutive-agreement) and records it.
   Re-taring before every dispense cancels the liquid already in the vial,
   so each reading is the net mass of a single dispense and one vial serves
   the whole run.
3. Per volume it converts the mean mass to a delivered volume (÷ water
   density) and reports accuracy (vs target) and precision, in a workbook
   `cv_mass_measurement_tip<GAUGE>G_YYYYmmdd_HHMMSS.xlsx`.

> **Valve port gotcha:** the bench valve is a Runze M05 **Bi-pass** valve
> with only two fluid states 90° apart. Firmware ports 1 & 3 land on the
> *same* state (and 2 & 4 on the other), so source and sink must be **90°
> apart, not 180°** — here `SOURCE_PORT = 2` (reservoir), `DISPENSE_PORT = 1`
> (tip). Using 3 & 1 silently aspirates and dispenses at the same tube. See
> [`LearnedPatterns.md`](LearnedPatterns.md) #1.

Metrics use standard metrology terms — **accuracy** (trueness vs the target,
signed: + over-dispense, − under-dispense) and **precision** (replicate
scatter):
> - **Systematic error (µL)** = `mean − target` — absolute accuracy
> - **Relative error (%)** = `100 · (mean − target) / target` — relative accuracy
> - **SD (µL)** = sample standard deviation — absolute precision
> - **CV (%)** = `100 · SD / mean` — coefficient of variation (= RSD), relative precision

### Workbook layout

The single sheet has a metadata block on top (timestamp, syringe tip,
syringe size, lab temperature, water density, and a legend for the four
metrics) followed by the result table — one row per target volume:

`Target Volume (µL) | Trial 1..N (g) | Mean Volume (µL) | Systematic error (µL) | Relative error (%) | SD (µL) | CV (%)`

Cell colouring:
- **Header row + Target Volume column** — light blue.
- **Systematic error / Relative error / SD / CV** — green→yellow→red
  heat-map by magnitude. Each metric has a band: fully green at/below its
  `*_GREEN` limit (acceptance deadband), fully red at/above its `*_RED`
  limit, yellow in between at `HEATMAP_MID_FRACTION` of the band. Sign is
  read from the number; colour reflects magnitude only. Stop colours are
  configurable too.

### Configuration

Edit the constants near the top of the script. Defaults:

| Constant | Default | Meaning |
|---|---|---|
| `SCALE_PORT` | `"110A:1150"` | Balance port — device path, USB `"VID:PID"` (resolved at runtime, survives renumbering), or `None` to auto-detect by Sartorius VID 0x24BC |
| `PUMP_PORT` / `PUMP_ADDRESS` | `"1A86:7523"` / `1` | SY-01B port (path or `"VID:PID"`) + bus address |
| `SYRINGE_UL` | `125` | Installed syringe volume |
| `SOURCE_PORT` / `DISPENSE_PORT` | `2` / `1` | Valve ports for aspirate / dispense (90° apart — see valve note) |
| `PRIME_CYCLES` / `PRIME_VOLUME_UL` | `2` / `125` | Pre-run full-stroke priming cycles |
| `SYRINGE_TIP_GAUGE` | `"30"` | Blunt-tip needle bore gauge (G); logged + in filename |
| `MEASUREMENT_PLAN` | `[(5,5),(50,5),(100,5)]` | `(target µL, replicates)` per volume |
| `BALANCE_AMBIENT` | `"very_unstable"` | Stability filter set over SBI (Esc K/L/M/N); looser = settles in a noisy env. `None` leaves the panel setting |
| `TARE_TOLERANCE_G` | `0.002` | Net weight still accepted as "zeroed" |
| `DISPENSE_SETTLE_S` | `3.0` | Initial grace pause after a dispense |
| `SETTLE_TOLERANCE_G` / `SETTLE_AGREEMENT_READS` | `0.001` / `3` | Settling: max spread / consecutive reads that must agree |
| `SETTLE_TIMEOUT_S` / `SETTLE_READ_POLL_S` | `30.0` / `6.0` | Settling: overall budget / per-read wait |
| `LAB_TEMP_C` | `18.0` | Laboratory temperature |
| `WATER_DENSITY_G_PER_ML` | `0.99860` | Pure-water density at `LAB_TEMP_C` |
| `SYS_ERR_*` / `REL_ERR_*` / `SD_*` / `CV_*` (`_GREEN`/`_RED`) | `0`→`1` µL / `0`→`5` % / `0`→`0.5` µL / `0`→`5` % | Heat-map green/red limits per metric |
| `HEATMAP_MID_FRACTION` | `0.5` | Where yellow sits in each green→red band (0..1) |
| `FILL_LIGHT_BLUE`, `HEATMAP_GOOD/MID/BAD` | pastels | Header/target fill + heat-map stop colours |

`MEASUREMENT_PLAN` is a list of `(target_volume_uL, replicates)` tuples —
add/remove rows to change which volumes are tested, change the second field
to set the trial count per volume (they may differ). E.g.
`[(10, 3), (20, 5), (100, 10)]`.

### Balance prerequisites (front panel)

The balance must be in SBI mode with stable-weight auto-push, set on the
front panel before running (see the `PrecisionScaleController` module
docstring for details). The ambient filter is set from code at startup
(`BALANCE_AMBIENT`), so only these two are menu-only:

- `STAB.RNG = V.FAST`
- `COM.OUTP = AUTO W/`

USB-C SBI defaults: 9600 baud, odd parity, 8 data bits, 1 stop bit.

### XZ stage (`xz_stage.py`)

Drives the XZ gantry's three MKS SERVO57D motors (one X + paired Z_A/Z_B)
via the `MKSServo57DCANController` driver over the FTDI USB2CAN adapters
(D2XX). On `home_and_position()` it: unbinds the kernel `ftdi_sio` driver
(so D2XX can claim the adapters), resolves each adapter by FTDI serial
(`SERIAL_X` is X, the other two are the synced Z), sets up, homes Z (paired,
parallel) then X, and moves to `(X_TARGET_MM, Z_TARGET_MM)` in `MOVE_ORDER`.

Config lives at the top of `xz_stage.py`: `SERIAL_X` (default `"NTAM63XD"`),
`X_TARGET_MM` (`261.5`), `Z_TARGET_MM` (`234.0`), `HOMING_SPEED_RPM`,
`MOVE_SPEED_PCT` / `MOVE_ACCEL_PCT`, `MOVE_ORDER`. List adapter serials with
`python -c "import ftd2xx; print(ftd2xx.listDevices())"`.

> ⚠️ **Moves a physical gantry.** Clear the frame and confirm the target +
> path are collision-free first. Verify `MOVE_ORDER` for your geometry. The
> paired Z motors have no desync interlock in this driver — keep an e-stop
> handy on first runs. Test it standalone before the full run:
> `python xz_stage.py`.

### Dependencies

Python ≥ 3.12, in the shared conda env **`sdl`** (new terminals activate
it). The pump and balance drivers are `pip install -e`'d into `sdl`, so
this cell only needs its own extras:

```bash
conda activate sdl
pip install -r requirements.txt   # openpyxl (+ ftd2xx for the XZ stage)
```

`ftd2xx` (for `xz_stage.py`) is a ctypes wrapper that also needs FTDI's
system **D2XX library** (`libftd2xx`) installed. If you don't use the XZ
stage, set `MOTOR_STAGE_ENABLE = False` and `ftd2xx` is never imported.

### Run

```bash
conda activate sdl
python cv_mass_measurement.py
```

The pump runs a read-only `diagnose()` and `initialize(force=2)` before the
operator prompt; abort safely at any time with `Ctrl-C` (partial results
are still written). **Only one process may own the pump's serial port** —
stop the `sy01b-server` (ESP32 bridge) before running this script.

## See also

- [`LearnedPatterns.md`](LearnedPatterns.md) — running log of gotchas hit
  on this bench (valve port mapping, balance settling, …). Read it before
  debugging; append new findings.
- [`CLAUDE.md`](CLAUDE.md) — conventions and environment for working in
  this folder.
