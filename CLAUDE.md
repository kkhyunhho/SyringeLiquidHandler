# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in the
**SyringeLiquidHandler** folder.

## Overview

Cross-project bench scripts that drive the **Runze SY-01B syringe pump**
and the **Sartorius Entris-II balance** together for gravimetric
liquid-handling measurements. This folder is the home for all combined
pump + balance work; new such scripts go here.

The scripts do **not** install the two device drivers — they import them
directly from the sibling project repos via a `sys.path` bootstrap at the
top of each script:

- `../PrecisionScaleController/PrecisionScaleController/src` → `entris_ii`
  (`PrecisionScaleController`)
- `../SyringePumpController/src` → `sy01b` (`SyringePumpController`)

The folder may be renamed/moved as long as it stays **one level under the
workspace root** (the bootstrap resolves paths from
`Path(__file__).resolve().parents[1]`).

## Conventions

For shared conventions — code style, the `sdl` env, testing, terminology
(**Level** = control-code depth; **Phase** = SDL hardware stage;
composition = device → **cell** → Phase-system), and task/commit rules —
see **SDLClaude** (`kkhyunhho/SDLClaude`), the single source of truth.

This folder is a **cell**: it composes the pump (`sy01b`) and balance
(`entris_ii`) drivers — both `pip install -e`'d into `sdl` — into a
gravimetric liquid-handling measurement, with an optional XZ motion stage.
Where this file is silent, SDLClaude governs.

## Files

| Path | Purpose |
|---|---|
| `cv_mass_measurement.py` | Gravimetric accuracy + precision (CV) of pump dispense volumes; writes a timestamped `.xlsx`. Optionally homes the XZ frame first. |
| `report.py` | Excel report rendering (heat-map styling + workbook writer), split out of `cv_mass_measurement.py`. |
| `xz_stage.py` | XZ gantry bring-up (MKS SERVO57D motors): home + move to the measurement position. |
| `server/` | FastAPI **L1 `/v1` server** — thin HTTP bridge over the cell (mirrors `sy01b-server`). See [WEB_V1_DRAFT.md](WEB_V1_DRAFT.md). |
| `cell.py` | `Cell` protocol + `CellError` hierarchy the server maps to HTTP. |
| `real_cell.py` | `SyringeCell` — real drivers behind `Cell` (pump/balance wired; stage `home` only, `move` pending the xz_stage→ESP32 mks_motor migration). |
| `tests/server/` | `FakeCell` + route tests (no hardware). |
| `README.md` | User-facing usage, configuration, workbook layout. |
| `requirements.txt` | `openpyxl` (+ `ftd2xx` for the standalone XZ motor). Pump/balance drivers come from the `sdl` env, not `sys.path`. |
| `LearnedPatterns.md` | Running log of gotchas (see below). |
| `example_cv_mass_measurement_tip30G.xlsx` | Sample output (fake data). |

- **Shared conda env `sdl`** (Python 3.12); new terminals activate it.
  The pump (`sy01b`) and balance (`entris_ii`) drivers are `pip install -e`'d
  into `sdl`, so [`cv_mass_measurement.py`](cv_mass_measurement.py) imports
  them directly — no `sys.path` bootstrap.
- **XZ stage motor:** [`xz_stage.py`](xz_stage.py) uses the
  **MKSServo57DCANController standalone** MKS driver (`ftd2xx`-based), which
  is *not* installed in `sdl` (its import name `mks_motor` collides with
  the full ESP32 driver). It is added to `sys.path` from that repo's `src/`.
  `ftd2xx` is installed in `sdl`.
- Runs in a Docker container; the container's `/dev` is a private tmpfs, so
  USB device nodes can go stale after re-enumeration / a Docker restart.

## Commands

| Purpose | Command |
|---|---|
| Run a measurement | `conda activate sdl && python cv_mass_measurement.py` |
| Run the /v1 server | `cp server/slh.toml.example server/slh.toml` then `python -m server` |
| Test the server | `python -m pytest tests/server/` (FakeCell, no hardware) |
| Lint | `ruff check cv_mass_measurement.py` |
| Format check | `ruff format --check cv_mass_measurement.py` |
| List serial ports | `python -m serial.tools.list_ports -v` |

New code must still satisfy the shared Python style
(80-col, 4-space, `snake_case`, Google-style docstrings, no magic numbers).

## Hardware & ports (this bench)

Identify devices by **stable USB VID:PID**, not `/dev/ttyUSB*` numbers
(which renumber across reboots). `cv_mass_measurement.py` resolves either a
device path or a `"VID:PID"` string at runtime (`_resolve_port`), and the
balance can also auto-detect by the Sartorius vendor ID.

| Device | VID:PID | Notes |
|---|---|---|
| Balance (Entris-II BCE224I) | `24BC:0010` | USB-C, Sartorius CDC → `ttyACM*`; `SCALE_PORT = None` auto-detects it. Must be passed into the container (`lsusb` shows `24bc:0010`). |
| Pump (SY-01B) | `1A86:7523` | CH340 USB-serial → `ttyUSB*`; `PUMP_PORT = "1A86:7523"`. |
| XZ motors (3× MKS SERVO57D) | `0403:6001` | FTDI USB2CAN adapters, addressed by **serial**: X = `NTAM63XD`, the other two (`A10PUO5V`, `A10PUO5W`) are the paired Z. Driven via D2XX (`ftd2xx`), see `xz_stage.py`. |
| Moxa UPort 1150 | `110A:1150` | A **different** instrument — NOT the balance. |
| ESP32-S3 | `303A:1001` | Other project; unrelated. |

### Valve port gotcha (critical)

The pump's valve is a Runze **M05 Bi-pass** valve with only **two** fluid
states 90° apart (`C-1/2-3` and `C-3/1-2`). Driven as a 4-way distribution
valve, firmware ports **1 & 3 map to the same fluid state** (and 2 & 4 to
the other) due to 180° rotor symmetry. So `move_valve_to_port` changing the
`?6` digit does **not** prove the fluid path changed. Source and sink must
be **90° apart, not 180°**: on this bench `SOURCE_PORT = 2` (reservoir),
`DISPENSE_PORT = 1` (tip). Verify with the eye (which tube moves liquid),
not `?6`. Full write-up in `LearnedPatterns.md` #1.

### Balance prerequisites (front panel, menu-only)

The Entris-II must be in SBI mode with stable-weight auto-push before a run
(not settable over the wire): `DEVICE → (USB or RS232) → DAT.REC = SBI`,
`DATA.OUT. → COM. SBI → COM.OUTP = AUTO W/`, `STAB.RNG = V.FAST`. A
balance returning `0x15` (NAK) to SBI commands is in xBPI mode — wrong
interface menu. SBI serial defaults: 9600 / ODD / 8 / 1.

## Folder-specific rules

1. **LearnedPatterns.md is mandatory.** Record every non-obvious problem
   solved here using the **Problem / Cause / Fix / Rule** format, newest at
   the bottom. Read it before debugging. (Standing user request.)

2. **Metric terminology is standard metrology** — do not relabel:
   - Accuracy (trueness vs target, signed): **Systematic error (µL)** =
     `mean − target`; **Relative error (%)** = `100·(mean − target)/target`.
   - Precision (replicate scatter): **SD (µL)**; **CV (%)** = `100·SD/mean`
     (the coefficient of variation ≡ RSD; always a percentage).
   - Never label an absolute error as "RSD".

3. **One owner of each serial port.** The pump's port can be held by either
   this script **or** the `sy01b-server` (ESP32 bridge), never both — stop
   the server before running standalone.

4. **Settling, not first-stable.** Post-dispense weights use
   `read_settled_weight` (wait for N consecutive in-tolerance readings), not
   a single auto-pushed value, which can fire before the liquid settles
   (LearnedPatterns #2).

5. **XZ stage is the highest-stakes subsystem.** `xz_stage.py` moves a
   physical gantry. The MKS driver (MKSServo57DCANController) has **no
   paired-Z desync interlock** (unlike the ESP32 variant), so a mid-move
   comms fault on one Z can rack the gantry. Always: test standalone
   (`python xz_stage.py`) with the frame clear before the full run; verify
   `MOVE_ORDER` is collision-free for the geometry; never auto-run the
   motors from a tool without the operator ready. D2XX (`ftd2xx`) needs
   `ftdi_sio` unbound first — `xz_stage.release_ftdi_sio()` does that; it
   only touches FTDI adapters, so the CH340 pump and CDC balance are
   unaffected.

## Research before coding

Before calling into `entris_ii` or `sy01b`, read the actual method in the
sibling repo's `src/` (and that repo's CLAUDE.md / LearnedPatterns) rather
than guessing — both drivers have hardware quirks documented there
(e.g. SY-01B `Q`-only busy polling, balance AUTO W/ jitter).
