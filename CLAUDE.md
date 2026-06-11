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

## Authority / conventions

This folder inherits the conventions of the two sibling repos it builds on:

- [`SyringePumpController/CLAUDE.md`](../SyringePumpController/CLAUDE.md)
  (which subordinates to upstream `coport-uni/CommonClaude`)
- [`PrecisionScaleController/CLAUDE.md`](../PrecisionScaleController/PrecisionScaleController/CLAUDE.md)
  (the canonical MIT-Python ruleset, §1–§17)

When this file is silent, follow those. Folder-specific rules below
specialize — they do not override the shared coding/commit/lint rules.

## Files

| Path | Purpose |
|---|---|
| `cv_mass_measurement.py` | Gravimetric accuracy + precision (CV) of pump dispense volumes; writes a timestamped `.xlsx`. |
| `README.md` | User-facing usage, configuration, workbook layout. |
| `requirements.txt` | `pyserial`, `openpyxl` (the two drivers load via `sys.path`, not pip). |
| `LearnedPatterns.md` | Running log of gotchas (see below). |
| `example_cv_mass_measurement_tip30G.xlsx` | Sample output (fake data). |

## Environment

- **Default conda env: `slh`** (Python 3.12 — meets sy01b's `>=3.12`
  floor). `conda activate slh`, then `pip install -r requirements.txt`.
- **VIRTUAL_ENV leak:** a stale `VIRTUAL_ENV=/workspace/AutomatedPipette/.venv`
  can shadow `python`/`pip` even inside `slh`. Always check
  `which python` resolves to `/opt/conda/envs/slh/bin/python`; if not,
  `deactivate` first.
- Runs in a Docker container; the container's `/dev` is a private tmpfs, so
  USB device nodes can go stale after re-enumeration / a Docker restart.

## Commands

| Purpose | Command |
|---|---|
| Run a measurement | `conda activate slh && python cv_mass_measurement.py` |
| Lint (if `ruff` installed) | `ruff check cv_mass_measurement.py` |
| Format check | `ruff format --check cv_mass_measurement.py` |
| List serial ports | `python -m serial.tools.list_ports -v` |

`ruff` is not always on PATH in this env; install with `pip install ruff`
into `slh` if needed. New code must still satisfy the shared Python style
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
| Moxa UPort 1150 | `110A:1150` | A **different** instrument — NOT the balance. |
| Motors (other project) | `0403:6001`, `303A:1001` | FTDI USB2CAN + ESP32; unrelated. |

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

## Research before coding

Before calling into `entris_ii` or `sy01b`, read the actual method in the
sibling repo's `src/` (and that repo's CLAUDE.md / LearnedPatterns) rather
than guessing — both drivers have hardware quirks documented there
(e.g. SY-01B `Q`-only busy polling, balance AUTO W/ jitter).
