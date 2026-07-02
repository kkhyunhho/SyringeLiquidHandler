# Adding a new cell

This repo is the SDLClaude **reference implementation** of a cell. To bring a
new hardware cell onto the same `/v1` web + server, copy the patterns here.
For the *why* (Level/Phase/cell terminology, the recursive HTTP substrate,
port-allocation rule) see SDLClaude `ARCHITECTURE.md`; this doc is the *how*.

A "cell" = the devices that must be **coordinated to move together** (the cell
boundary rule). Each cell is one process: a FastAPI `/v1` server wrapping one
`Cell` implementation that drives the devices.

## Steps

### 1. Vendor your driver
Copy the device's L0 driver into `vendor/<codename>/` (a package) — same as
`vendor/sy01b/`, `vendor/mks_motor/`, etc. Keep the upstream files verbatim;
put any local shim (e.g. VID:PID resolution like `vendor/lmc/`) in a separate
module. Record the source repo + commit + any local change in
`vendor/VENDORED.md`. Add the driver's runtime deps to `requirements.txt`.

### 2. Write `cell/<your>_cell.py`
Copy `cell/pump_gantry_cell.py` (or `balance_linear_cell.py`) as a template
and implement the `Cell` protocol (`cell/cell_protocol.py`). The methods fall
into **two layers** (see SDLClaude `ARCHITECTURE.md` → "Cell contract"):

| Layer | Group | Methods |
|---|---|---|
| **Substrate** (universal) | Discovery | `diagnose() -> dict`, `status() -> dict` |
| **Substrate** (universal) | Lifecycle | `stop()`, `close()`, classmethod `open(config)` |
| **Action set** (per family) | Balance | `tare()`, `read_weight()`, `set_ambient(level)` |
| **Action set** (per family) | Pump | `initialize()`, `move_valve()`, `aspirate()`, `dispense()`, `cycle()` |
| **Action set** (per family) | Gantry | `home_gantry()`, `move_gantry(x, z, *, speed_pct, accel_pct)` |
| **Action set** (per family) | Linear | `home_linear()`, `move_linear(y_mm)` |

Rules:
- **Always implement the Substrate** (discovery + lifecycle) — that's what the
  orchestrator/web use for every cell.
- **Implement the action sets your hardware has;** for a device family this
  cell does NOT have, `raise WrongStateError(...)` (see the defensive stubs
  `PumpGantryCell._no_balance` / `BalanceLinearCell._no_pump`). The web greys
  those out from `diagnose()` presence flags.
- **New motion/product family → new action set, don't overload an existing
  one.** `gantry` (XZ, CAN via `mks_motor`) and `linear` (Y, RS-485 via `lmc`)
  are separate action sets — different motors, axes, and wire protocols, so a
  shared "stage" signature would lean toward one and misfit the other. A
  genuinely different product gets **its own** action set + `/v1` routes,
  conforming to the Substrate only — never crammed into pump/balance/gantry/
  linear.
- **A robot arm is just another action family — it does NOT break the cell
  format.** The planned arm is operated by hardcoded trajectories triggered as
  discrete named actions (e.g. `run_trajectory("A"|"B"|"C")`, surfaced as A/B/C
  buttons in the web), not a continuous pose/grip interface. That is still a
  normal cell: it implements the same Substrate (health/diagnose/status,
  lifecycle, error envelope, one `/v1` server on its own port) and simply adds
  an `arm` action set with its own routes. The Substrate is what makes it
  compose with every other cell; the discrete-trajectory action set is the only
  part that's arm-specific.
- `open(config)` is the **composition root**: open the drivers, run any
  one-time setup, return the instance. Hold drivers as attributes (`has-a`);
  translate name/unit/order in the method bodies (Adapter pattern).
- Intra-cell imports are relative (`from .cell_protocol import ...`); driver
  imports are absolute (`from vendor.<codename> import ...`).

### 3. Raise the right `CellError` — it maps to HTTP automatically
`server/errors.py` maps each subclass to a status code, so just raise the
correct one and the web gets a stable error envelope:

| Exception | HTTP | When |
|---|---|---|
| `InvalidArgError` | 400 | bad argument (out of range, unknown level) |
| `WrongStateError` | 409 | not initialized / device absent / wrong order |
| `DeviceFaultError` | 500 | hardware fault (overload, init failure) |
| `TransportError` | 503 | serial/CAN link down |
| `CellTimeoutError` | 504 | device didn't respond in time |

### 4. Add a config + loader
- Define a `@dataclass(frozen=True, slots=True)` config (ports, serials) in
  your cell module, like `Config` / `BalanceLinearConfig`.
- Add a `_load_<shape>()` in `server/__main__.py` that parses the TOML tables
  into that config (mirror `_load` / `_load_balance_linear`).
- Add a `server/cell<N>.toml.example` (real `.toml` is gitignored). Resolve
  device addresses by **VID:PID**, not `/dev/ttyUSBn` (renumbers).

### 5. Wire the `--cell` flag
In `server/__main__.py`: add your shape to the `--cell` `choices`, and a
branch that calls your `_load_<shape>()` + `YourCell.open(cfg)` as the
factory passed to `create_app`.

### 6. Assign a port
Per the SDLClaude port table, one port per `/v1` server (cell1=17054,
cell2=17056, …). Put it in your `[server] port`.

### 7. Lint, then bring up at the bench
Verification is hardware-in-the-loop — there is no in-memory fake. Lint
first, then bring the cell up against the real devices with an operator
ready and an e-stop handy (see the safety rules in `CLAUDE.md`):
```bash
ruff check cell/ server/
python -m server --config server/cellN.toml
# then GET /v1/health, GET /v1/diagnose before any motion command
```

### 8. Register in the web (when wiring the UI)
Add the cell to the web's cell registry (`web/src/lib/cells.ts`) with its
base URL; the operator web's switcher picks it up. Adding a cell = a registry
entry, not a new site.

## Checklist
- [ ] driver in `vendor/<codename>/` + `VENDORED.md` + `requirements.txt`
- [ ] `cell/<your>_cell.py` implements all `Cell` methods (absent → raise)
- [ ] correct `CellError` subclasses raised
- [ ] config dataclass + `_load_<shape>()` + `cell<N>.toml.example`
- [ ] `--cell` choice + factory branch in `server/__main__.py`
- [ ] port assigned
- [ ] `ruff check cell/ server/` passes; cell brought up at the bench (health + diagnose)
- [ ] cell registered in `web/src/lib/cells.ts`
