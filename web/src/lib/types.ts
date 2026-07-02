// Domain types (mock data; mirror the /v1 contract).

export type Conn = "ok" | "fault" | "idle"
// Physical valve ports as the operator names them: aspirate/dispense go
// through the syringe common C, which reaches only Port 1 (state C↔1) or
// Port 3 (state C↔3). Port 2 is a bypass port and is never used. NOTE: the
// pump's firmware command for Port 3 is I2R, not I3R (I3R = C↔1 = Port 1) —
// the physical→firmware mapping lives in `valveFw` in App.tsx.
export type Port = 1 | 3
export type Dev = "pump" | "balance" | "stage"

export interface Live {
  weightG: number
  path: 1 | 2
  aspPort: Port
  dispPort: Port
  valveConnect: Port // port currently joined to C (changes only on Activation)
  plungerUL: number
  stageXmm: number
  stageZmm: number
  error: string | null
}

export type Op =
  | { kind: "dispense"; v: number; asp: Port; disp: Port; path: 1 | 2; pumpPct: number }
  | { kind: "stage"; x: number; z: number; sPct: number; aPct: number }
  | { kind: "prime"; n: number; pumpPct: number; src: Port; disp: Port }
  | { kind: "tare" }

export interface Step {
  id: number
  label: string
  op: Op
}

// A replayable command, recorded on each History entry so a saved scenario
// (a span of History) can be re-executed on the right cell.
export type ReplayAction =
  | { kind: "diagnose"; cell: string }
  | { kind: "initialize"; cell: string }
  | { kind: "tare"; cell: string }
  | { kind: "calibrate"; cell: string }
  | { kind: "ambient"; cell: string; level: string }
  | { kind: "dispense"; cell: string; op: Extract<Op, { kind: "dispense" }> }
  | { kind: "prime"; cell: string; op: Extract<Op, { kind: "prime" }> }
  | { kind: "stage"; cell: string; op: Extract<Op, { kind: "stage" }> }
  | { kind: "home"; cell: string }
  | { kind: "linear"; cell: string; y: number }

export interface HistEntry {
  id: number
  at: string
  label: string
  action?: ReplayAction
}
