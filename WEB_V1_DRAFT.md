# SyringeLiquidHandler — `/v1` contract + Web UI (draft v1)

Design spec for this cell's L1 server and its web front-end. API-first:
endpoints are derived from the L0 driver capabilities (pump `sy01b`,
balance `entris_ii`, XZ stage via the **ESP32 full `mks_motor`** driver).
See SDLClaude/ARCHITECTURE.md for terminology (Level / Phase / cell).

## Principles
- API-first; mirror the `sy01b-server` pattern.
- Single in-flight (`asyncio.Lock`); **no auto-init** (client must call it);
  stable error envelope `{error, code, command, message}`.
- Discovery = read-only, repeat-safe (GET); Motion = POST.
- The XZ stage is backed by the **ESP32 full `mks_motor`** (pyftdi,
  paired-axis interlock), NOT the MKSServo standalone. (Driver migration of
  xz_stage.py is a build-time task — tracked separately.)

## Endpoints

| Group | Method · Path | Body | Reply |
|---|---|---|---|
| Discovery | `GET /v1/health` | — | `{cell_up, pump_ok, balance_ok, stage_ok}` |
| | `GET /v1/diagnose` | — | pump diagnose + balance ID/voltage + stage status |
| | `GET /v1/status` | — | `{weight_g, valve, plunger_uL, stage_x_mm, stage_z_mm, busy, error}` (2 s poll) |
| Balance | `GET /v1/balance/weight` | — | `{weight_g, stable}` (settled read) |
| | `POST /v1/balance/tare` | — | `{weight_g}` |
| | `POST /v1/balance/ambient` | `{level}` | `{level}` — very_stable…very_unstable |
| Pump | `POST /v1/pump/initialize` | `{force=2, ccw=false}` | `{valve, plunger_uL}` |
| | `POST /v1/pump/valve` | `{port}` | `{valve}` |
| | `POST /v1/pump/aspirate` | `{target_uL}` | `{plunger_uL}` |
| | `POST /v1/pump/dispense` | `{target_uL=0}` | `{plunger_uL}` |
| | `POST /v1/pump/cycle` | `{cycles, volume_uL, source_port, dispense_port}` | `{cycles_done, final_valve}` |
| Stage (required) | `POST /v1/stage/home` | — | `{x_mm, z_mm}` |
| | `POST /v1/stage/move` | `{x_mm, z_mm}` | `{x_mm, z_mm}` |
| Measurement | `POST /v1/measure/run` | `{plan:[[uL,n],…], source_port, dispense_port, prime_cycles}` | `{job_id}` |
| | `GET /v1/measure/status` | — | `{state, current:[uL,rep], progress, partial}` |
| | `GET /v1/measure/result` | — | per-volume `{uL, mean_uL, systematic_err_uL, systematic_err_pct, cv_pct}` |
| | `POST /v1/measure/stop` | — | `{stopped}` |
| | `GET /v1/measure/export` | — | `.xlsx` download |
| Safety | `POST /v1/stop` | — | abort all motion (pump + stage) |

**`cycle`** = the unified repeated aspirate→dispense: one cycle = aspirate
`volume_uL` from `source_port`, dispense to `dispense_port`. Serves both
priming (many cycles, discarded) and dispensing, by volume/cycle count.
`aspirate` / `dispense` remain as single-step primitives for manual control.

**Measurement is async**: `run` returns a `job_id`; poll `status`; fetch
`result` when done. A run = for each `(uL, n)` in the plan: cycle/dispense
`uL`, settle-weigh `n` times, compute mean / systematic error / CV.

## Button inventory (→ endpoint, gating)

| Button | → endpoint | gating |
|---|---|---|
| Diagnose / Reconnect | `/diagnose` | always |
| Tare | `/balance/tare` | connected |
| Ambient ▾ | `/balance/ambient` | connected |
| Initialize | `/pump/initialize` | after diagnose OK |
| Valve 1·2·3·4 | `/pump/valve` | after init |
| µL + Aspirate / Dispense | `/pump/aspirate` · `/dispense` | after init |
| Cycle (cycles, volume) | `/pump/cycle` | after init |
| Home / X·Z + Move | `/stage/home` · `/move` | after diagnose |
| Plan edit + Run / Stop | `/measure/run` · `/stop` | after init + tare |
| Export | `/measure/export` | when results exist |
| STOP (large red) | `/stop` | always (top-right, fixed) |

## Web layout (wireframe)

High-performance-HMI: gray base, color only for alarms, status always
visible. Right side = device control **tabs** (Balance / Pump / Stage);
Status is the top bar, not a tab.

```
┌─────────────────────────────────────────────────────────────────────┐
│ SyringeLiquidHandler   ●pump ●balance ●stage   [Diagnose] [■ STOP]    │
├───────────────────────────────┬─────────────────────────────────────┤
│  LIVE                          │  CONTROLS  [ Balance | Pump | Stage ]│
│  weight   0.0000 g             │  (Pump tab e.g.)                     │
│  valve: 2   plunger: 0 µL      │  [Initialize]                       │
│  stage: X 261.5  Z 234.0       │  Valve [1][2][3][4]                 │
│                                │  Volume [__] µL [Aspirate][Dispense]│
│  ── MEASUREMENT ──             │  Cycle  cycles[3] vol[125] [Cycle]  │
│  Plan: 100 µL × 5  [edit]      │                                     │
│  [ Run ] [ Stop ]   ▓▓▓▓░ 3/5  │                                     │
│  ┌ results ─────────────────┐  │                                     │
│  │ µL  mean SE(µL) SE%  CV%  │  │                                     │
│  │100  98.8 -1.2  -1.2  0.9  │  │   [Export .xlsx]                    │
│  └───────────────────────────┘ │                                     │
└───────────────────────────────┴─────────────────────────────────────┘
```

- Left = primary task (Measurement) + live readouts. Right tabs = manual /
  commissioning control. STOP fixed top-right. Initialize gated on diagnose;
  Run gated on init + tare.

## Open / deferred
- Tabs vs accordion for the right panel — defaulting to **tabs**; revisit
  while building.
- Build order: this contract → install official remote Figma MCP → design
  & build the **WEB** first, over this contract.
- Driver task: rewrite `xz_stage.py` onto the ESP32 full `mks_motor`
  (pyftdi) at L1-server build time; confirm the bench adapter under pyftdi.
