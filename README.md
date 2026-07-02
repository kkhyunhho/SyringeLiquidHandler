# SyringeLiquidHandler

The Phase-1 **cell** project: hardware drivers composed behind a `Cell`
interface, served over an L1 FastAPI `/v1` API, with a React web UI. It is
the SDLClaude reference for two cell shapes:

- **pump + gantry cell** (cell1–3): syringe pump (`sy01b`) + XZ gantry (ESP32
  `mks_motor`, paired-Z interlock).
- **balance + linear cell** (cell4): MINAS A6 linear rail (`lmc`) + the single
  Entris-II balance (`entris_ii`) that shuttles under cell1–3.

All hardware drivers are **vendored** in-repo (`vendor/`), so the project runs
standalone — see [`vendor/VENDORED.md`](vendor/VENDORED.md) for upstream
sources and commits.

## L1 server, cells & web

```
web (React, web/)  ──HTTP /v1──▶  server/ (FastAPI)  ──▶  Cell  ──▶  drivers ──▶ hardware
```

| Piece | What |
|---|---|
| [`cell/`](cell/) | the cell layer (package): interface + implementations |
| [`cell/cell_protocol.py`](cell/cell_protocol.py) | the `Cell` interface + `CellError` hierarchy (→ HTTP status) |
| [`cell/pump_gantry_cell.py`](cell/pump_gantry_cell.py) | `PumpGantryCell` — pump + XZ gantry (ESP32 `mks_motor`) |
| [`cell/balance_linear_cell.py`](cell/balance_linear_cell.py) | `BalanceLinearCell` — MINAS A6 linear (`lmc`) + balance |
| [`vendor/lmc/`](vendor/lmc/) | codename `lmc` — VID:PID shim over the vendored MINAS A6 raw driver |
| [`server/`](server/) | `create_app` + `/v1` routes + schemas + error mapping + `__main__` |
| [`web/`](web/) | the operator UI (one web for the whole SDL; cell switcher) |
| [`vendor/`](vendor/) | all hardware drivers, vendored in-repo (see `VENDORED.md`) |

```bash
# run a cell server (real hardware)
python -m server --config server/cell1.toml                          # cell1 (pump+gantry)    :17054
python -m server --config server/cell4.toml                          # cell4 (balance+linear) :17060

# web (dev): npm run dev in web/, open the forwarded :5173
```

Ports are per-cell (SDLClaude ARCHITECTURE.md): cell1=17054 … cell4=17060.
The web proxies `/api/cell1`→17054 and `/api/cell4`→17060 (cell2/3 are mock).

## Bench notes

### Valve port gotcha (critical)

The pump's valve is a Runze M05 **Bi-pass** valve with only two fluid states
90° apart. Firmware ports 1 & 3 land on the *same* state (and 2 & 4 on the
other), so source and sink must be **90° apart, not 180°** — on this bench
the reservoir is port 2 and the tip is port 1. Using 3 & 1 silently aspirates
and dispenses at the same tube. Verify with the eye (which tube moves liquid),
not the `?6` digit. See [`LearnedPatterns.md`](LearnedPatterns.md) #1.

### Balance prerequisites (front panel, menu-only)

The Entris-II must be in SBI mode with stable-weight auto-push before use
(not settable over the wire):

- `DAT.REC = SBI`, `COM.OUTP = AUTO W/`, `STAB.RNG = V.FAST`

USB-C SBI defaults: 9600 baud, odd parity, 8 data bits, 1 stop bit. A balance
returning `0x15` (NAK) is in xBPI mode — wrong interface menu. The ambient
(vibration) filter is set from the cell config (`ambient`), not the panel.

## Dependencies

Python ≥ 3.12, in the shared conda env **`sdl`** (new terminals activate it).
All hardware drivers are vendored in-repo (`vendor/`), so a fresh install only
needs their runtime deps + the server stack (no GitHub / sibling clones):

```bash
conda activate sdl
pip install -r requirements.txt   # pyserial/pyftdi + fastapi/uvicorn/httpx
```

## See also

- **SDLClaude `ARCHITECTURE.md`** — the SDL-wide architecture (Levels /
  Phases / cells, the cell boundary rule, the recursive HTTP `/v1` substrate,
  port table, and the orchestrator→hardware diagram). Read it for how this
  cell fits the whole lab; this repo is one cell within it.
- [`LearnedPatterns.md`](LearnedPatterns.md) — running log of gotchas hit on
  this bench (valve port mapping, balance settling, gantry homing …). Read it
  before debugging; append new findings.
- [`ADDING_A_CELL.md`](ADDING_A_CELL.md) — step-by-step for bringing a new
  hardware cell onto this `/v1` server + web (start here to add hardware).
- [`CLAUDE.md`](CLAUDE.md) — conventions and environment for working here.
- [`vendor/VENDORED.md`](vendor/VENDORED.md) — vendored driver sources + commits.
