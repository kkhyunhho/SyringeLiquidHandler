// Typed client for the /v1 server. The browser calls /v1/* same-origin;
// Vite's dev proxy (vite.config.ts) forwards it to the FastAPI server.
//
// Every call returns the parsed JSON body, or throws ApiError carrying the
// server's stable error envelope ({error, code, command, message}) plus the
// HTTP status — so callers can show the message and branch on status.

export class ApiError extends Error {
  status: number
  code: number | null
  command: string | null
  errorName: string
  constructor(
    status: number,
    body: { error?: string; code?: number | null; command?: string | null; message?: string },
  ) {
    super(body.message || `HTTP ${status}`)
    this.name = "ApiError"
    this.status = status
    this.code = body.code ?? null
    this.command = body.command ?? null
    this.errorName = body.error || "Error"
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, body)
  return body as T
}

const get = <T>(path: string) => req<T>(path)
const post = <T>(path: string, json?: unknown) =>
  req<T>(path, {
    method: "POST",
    body: json !== undefined ? JSON.stringify(json) : undefined,
  })

// ── Response shapes (mirror server/schemas.py) ─────────────────────────────
export interface Health {
  cell_up: boolean
  pump_ok: boolean | null
  balance_ok: boolean | null
  stage_ok: boolean | null
  driver_versions: Record<string, string>
}
export interface Diagnose {
  pump: Record<string, unknown>
  balance: Record<string, unknown>
  stage: Record<string, unknown>
  ok_to_initialize: boolean
}
export interface Status {
  weight_g: number
  valve: string
  plunger_uL: number
  stage_x_mm: number
  stage_z_mm: number
  busy: boolean
  error: string | null
}

// ── Endpoints ──────────────────────────────────────────────────────────────
export const api = {
  health: () => get<Health>("/v1/health"),
  diagnose: () => get<Diagnose>("/v1/diagnose"),
  status: () => get<Status>("/v1/status"),

  tare: () => post<{ weight_g: number }>("/v1/balance/tare"),
  ambient: (level: string) =>
    post<{ level: string }>("/v1/balance/ambient", { level }),

  initialize: (force = 2) =>
    post<{ valve: string; plunger_uL: number }>("/v1/pump/initialize", {
      force,
    }),
  valve: (port: number) => post<{ valve: string }>("/v1/pump/valve", { port }),
  aspirate: (target_uL: number) =>
    post<{ plunger_uL: number }>("/v1/pump/aspirate", { target_uL }),
  dispense: (target_uL = 0) =>
    post<{ plunger_uL: number }>("/v1/pump/dispense", { target_uL }),
  cycle: (cycles: number, volume_uL: number, source_port: number, dispense_port: number) =>
    post<{ cycles_done: number; final_valve: string }>("/v1/pump/cycle", {
      cycles,
      volume_uL,
      source_port,
      dispense_port,
    }),

  stageHome: () => post<{ x_mm: number; z_mm: number }>("/v1/stage/home"),
  stageMove: (x_mm: number, z_mm: number, speed_pct: number, accel_pct: number) =>
    post<{ x_mm: number; z_mm: number }>("/v1/stage/move", {
      x_mm,
      z_mm,
      speed_pct,
      accel_pct,
    }),

  stop: () => post<{ stopped: boolean }>("/v1/stop"),
}
