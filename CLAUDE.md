# CLAUDE.md

This file guides Claude Code (claude.ai/code) when working in the
**SyringeLiquidHandler** folder.

## Overview

The Phase-1 **cell** project: device drivers composed behind a `Cell`
interface, exposed over an L1 FastAPI `/v1` server, with a React web UI. Two
cell shapes live here (the SDLClaude reference implementations):

- **pump + gantry cell** (cell1–3): syringe pump (`sy01b`) + XZ gantry (the
  ESP32 `mks_motor`, paired-Z interlock). No balance.
- **balance + linear cell** (cell4): MINAS A6 linear rail (`lmc`) + the
  **single** Entris-II balance (`entris_ii`) that shuttles under cell1–3 to
  weigh each dispense.

**Self-contained (fully vendored).** All four hardware drivers are copied
in-repo under `vendor/` — `sy01b`, `entris_ii`, `mks_motor` as packages and
the MINAS A6 `linear_motor_controller` as a flat module — and imported as
`vendor.<name>`. A fresh `pip install -r requirements.txt` needs no GitHub
access or sibling clones; only the drivers' runtime deps (`pyserial`,
`pyftdi`) come from PyPI. Upstream sources, commits, and the local changes
applied are tracked in `vendor/VENDORED.md`; update a driver by re-copying
its source. (The dev `sdl` env may still have the drivers as editable
installs, but the cell imports the vendored copies.)

## Conventions

For shared conventions — code style, the `sdl` env, testing, terminology
(**Level** = control-code depth; **Phase** = SDL hardware stage;
composition = device → **cell** → Phase-system), and task/commit rules —
see **SDLClaude** (`kkhyunhho/SDLClaude`), the single source of truth.
Where this file is silent, SDLClaude governs.

## Files

| Path | Purpose |
|---|---|
| `cell/` | the **cell layer** (package): the `Cell` interface + cell implementations. |
| `cell/cell_protocol.py` | `Cell` protocol + `CellError` hierarchy the server maps to HTTP. |
| `cell/pump_gantry_cell.py` | `PumpGantryCell` (cell1–3) — pump (`sy01b`) + XZ gantry (ESP32 `mks_motor`, paired-Z interlock), no balance. |
| `cell/balance_linear_cell.py` | `BalanceLinearCell` — real cell4: MINAS A6 linear rail (`lmc`) + Entris-II balance, no pump. Run with `python -m server --config server/cell4.toml` (shape auto-detected from the `[linear]` table). |
| `vendor/lmc/` | Codename `lmc` package — VID:PID-resolving shim (`__init__.py`) over the vendored MINAS A6 raw driver (`linear_motor_controller.py`, RS485 standard protocol with a PID closed loop); imported as `vendor.lmc`. |
| `server/` | FastAPI **L1 `/v1` server** — thin HTTP bridge over the cell (mirrors `sy01b-server`). |
| `vendor/` | All hardware drivers vendored in-repo (`sy01b`, `entris_ii`, `mks_motor`, `linear_motor_controller`); see `vendor/VENDORED.md` for sources/commits. |
| `README.md` | User-facing usage + bench notes. |
| `ADDING_A_CELL.md` | Step-by-step guide to add a new hardware cell (the *how*; SDLClaude has the *why*). |
| `requirements.txt` | Runtime deps only (`pyserial`, `pyftdi`, `fastapi`, …); the drivers themselves are vendored. |
| `LearnedPatterns.md` | Running log of gotchas (see below). |

- **Shared conda env `sdl`** (Python 3.12); new terminals activate it. All
  drivers are vendored under `vendor/` and imported as `vendor.<name>`, so
  the cell needs no `sys.path` bootstrap (the repo root is already importable).
- Runs in a Docker container; the container's `/dev` is a private tmpfs, so
  USB device nodes can go stale after re-enumeration / a Docker restart. The
  `mks_motor` driver's `prepare_usb_nodes()` / `release_ftdi_sio()` rebuild
  the FTDI nodes + detach `ftdi_sio` at startup.

## Commands

| Purpose | Command |
|---|---|
| Run a cell server | `cp server/cell1.toml.example server/cell1.toml` then `python -m server --config server/cell1.toml` |
| Lint | `ruff check pump_gantry_cell.py balance_linear_cell.py server/` |
| Format check | `ruff format --check .` |
| List serial ports | `python -m serial.tools.list_ports -v` |

New code must still satisfy the shared Python style
(80-col, 4-space, `snake_case`, Google-style docstrings, no magic numbers).

## Hardware & ports (this bench)

Identify devices by **stable USB VID:PID**, not `/dev/ttyUSB*` numbers
(which renumber across reboots). The cell configs take either a device path
or a `"VID:PID"` string, resolved at runtime; the balance can also
auto-detect by the Sartorius vendor ID.

| Device | VID:PID | Notes |
|---|---|---|
| Balance (Entris-II BCE224I) | `24BC:0010` | USB-C, Sartorius CDC → `ttyACM*`; omit `scale_port` to auto-detect. Must be passed into the container (`lsusb` shows `24bc:0010`). |
| Pump (SY-01B) | `1A86:7523` | CH340 USB-serial → `ttyUSB*`; `pump.port = "1A86:7523"`. |
| XZ motors (3× MKS SERVO57D) | `0403:6001` | FTDI USB2CAN adapters, addressed by **serial**: X = `NTAM63XD`, the other two (`A10PUO5V`, `A10PUO5W`) are the paired Z. Driven via pyftdi (`mks_motor`), vendored from **ESP32S3BOX3MotorController** (the sole XZ gantry reference). |
| MINAS A6 linear rail (`lmc`) | `110A:1150` | Moxa UPort 1150 RS-485 adapter is the linear's serial link (NOT a TI USB3410, NOT the balance). `linear_port = "110A:1150"` resolved at runtime by `vendor.lmc.resolve_port`. |
| ESP32-S3 | `303A:1001` | Other project; unrelated. |

### Valve port gotcha (critical)

The pump's valve is a Runze **M05 Bi-pass** valve with only **two** fluid
states 90° apart (`C-1/2-3` and `C-3/1-2`). Driven as a 4-way distribution
valve, firmware ports **1 & 3 map to the same fluid state** (and 2 & 4 to
the other) due to 180° rotor symmetry. So `move_valve_to_port` changing the
`?6` digit does **not** prove the fluid path changed. Source and sink must
be **90° apart, not 180°**: on this bench the reservoir is port 2 and the
tip is port 1. Verify with the eye (which tube moves liquid), not `?6`. Full
write-up in `LearnedPatterns.md` #1.

### Balance prerequisites (front panel, menu-only)

The Entris-II must be in SBI mode with stable-weight auto-push before a run
(not settable over the wire): `DEVICE → (USB or RS232) → DAT.REC = SBI`,
`DATA.OUT. → COM. SBI → COM.OUTP = AUTO W/`, `STAB.RNG = V.FAST`. A
balance returning `0x15` (NAK) to SBI commands is in xBPI mode — wrong
interface menu. SBI serial defaults: 9600 / ODD / 8 / 1.

### MINAS A6 amp prerequisites (linear rail)

The linear (Y) rail runs on the **MINAS standard serial protocol over RS485**
(`vendor.lmc.LinearMotorController`), not Modbus. Amp parameters: `Pr5.37 = 0`
(standard protocol), `Pr5.30 = 2` (9600 bps), `Pr5.31 = 1` (slave ID); serial
9600 / 8N1. `move_to_mm` runs a software closed loop whose per-iteration speed
is set by the driver's `PIDController` (P-tuned; see `linear_motor_controller.py`
`class PIDController`), converging to ±0.1 mm and aborting if the residual
stops shrinking — this replaced the earlier fixed step schedule and fixes the
overshoot without switching to Modbus.

## Folder-specific rules

1. **LearnedPatterns.md is mandatory.** Record every non-obvious problem
   solved here using the **Problem / Cause / Fix / Rule** format, newest at
   the bottom. Read it before debugging. (Standing user request.)

2. **One owner of each serial port.** A device's port can be held by only one
   process at a time — stop the cell server before any standalone use of the
   same pump/balance/motor port (and vice versa).

3. **XZ gantry is the highest-stakes subsystem.** `PumpGantryCell` moves a
   physical gantry via the ESP32 `mks_motor` driver. That driver *does* have
   a paired-Z desync interlock (`move_sync` / `home_xz` / `stop_group_hard`),
   so always drive the gantry through those high-level group helpers — never
   `MKSMotor._send` directly, which also bypasses the `_is_at_limit()`
   pre-send that absorbs the MKS firmware's "first motion command after a
   limit-stop is dropped" quirk. Clear the frame and keep an e-stop handy on
   first runs; never auto-run the motors from a tool without the operator
   ready. The driver's `release_ftdi_sio()` detaches the kernel `ftdi_sio`
   (FTDI adapters only — the CH340 pump and CDC balance are unaffected).

## Research before coding

Before calling into a driver, read its actual method in `vendor/<name>/`
(and that driver's upstream CLAUDE.md / LearnedPatterns) rather than
guessing — they have hardware quirks documented there (e.g. SY-01B `Q`-only
busy polling, balance AUTO W/ jitter, MKS first-command drop at limits).
