// Domain types (mock data; mirror the /v1 contract).

export type Conn = "ok" | "fault" | "idle"
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

export interface HistEntry {
  id: number
  at: string
  label: string
}
