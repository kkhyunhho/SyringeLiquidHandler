// In-memory stand-in for the /v1 client, used for cells whose hardware/
// backend isn't wired yet (cell2/cell3) or to preview the whole UI with no
// server. Mirrors the real `api` surface (ApiClient) with plain state, so a
// cell view can't tell the difference. Calls resolve fast; the cell view's
// withAnim() drives the visible animation timing.

import type { ApiClient, Diagnose, Health, Status } from "@/lib/api"

export function makeMockClient(): ApiClient {
  // Per-instance state (one mock backend per cell).
  const s = {
    weight_g: 0,
    valve: "?",
    plunger_uL: 0,
    x_mm: 0,
    z_mm: 0,
    initialized: false,
  }
  const asPort = (p: number) => String(p)

  return {
    health: async (): Promise<Health> => ({
      cell_up: true,
      pump_ok: s.initialized || null,
      balance_ok: true,
      stage_ok: true,
      driver_versions: { pump: "MOCK", balance: "MOCK" },
    }),
    diagnose: async (): Promise<Diagnose> => ({
      pump: { software_version: "MOCK", valve: s.valve, ok: true },
      balance: { model: "MOCK", ok: true },
      stage: { enabled: true, ok: true },
      ok_to_initialize: true,
    }),
    status: async (): Promise<Status> => ({
      weight_g: s.weight_g,
      valve: s.valve,
      plunger_uL: s.plunger_uL,
      stage_x_mm: s.x_mm,
      stage_z_mm: s.z_mm,
      busy: false,
      error: null,
    }),
    tare: async () => {
      s.weight_g = 0
      return { weight_g: 0 }
    },
    calibrate: async () => {
      // isoCAL / internal calibration: empty-pan zero baseline.
      s.weight_g = 0
      return { weight_g: 0 }
    },
    weight: async () => {
      // Mock a small settled reading so the button visibly does something.
      s.weight_g = Math.round((Math.random() * 2 + 0.05) * 1000) / 1000
      return { weight_g: s.weight_g, stable: true }
    },
    ambient: async (level: string) => ({ level }),
    initialize: async () => {
      s.initialized = true
      s.valve = "1"
      s.plunger_uL = 0
      return { valve: s.valve, plunger_uL: 0 }
    },
    valve: async (port: number) => {
      s.valve = asPort(port)
      return { valve: s.valve }
    },
    aspirate: async (target_uL: number) => {
      s.plunger_uL = target_uL
      return { plunger_uL: target_uL }
    },
    dispense: async (target_uL = 0) => {
      s.plunger_uL = target_uL
      return { plunger_uL: target_uL }
    },
    cycle: async (
      cycles: number,
      _volume_uL: number,
      _source_port: number,
      dispense_port: number,
    ) => {
      s.valve = asPort(dispense_port)
      s.plunger_uL = 0
      return { cycles_done: cycles, final_valve: s.valve }
    },
    gantryHome: async () => {
      s.x_mm = 0
      s.z_mm = 0
      return { x_mm: 0, z_mm: 0 }
    },
    gantryMove: async (
      x_mm: number,
      z_mm: number,
      _speed_pct: number,
      _accel_pct: number,
    ) => {
      s.x_mm = x_mm
      s.z_mm = z_mm
      return { x_mm, z_mm }
    },
    linearHome: async () => {
      s.x_mm = 0
      return { y_mm: 0 }
    },
    linearMove: async (y_mm: number) => {
      s.x_mm = y_mm
      return { y_mm }
    },
    stop: async () => ({ stopped: true }),
  }
}
