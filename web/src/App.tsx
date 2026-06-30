import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  Activity,
  Beaker,
  Droplet,
  Eye,
  ListChecks,
  Move3d,
  OctagonX,
  Play,
  Plus,
  RefreshCw,
  RotateCw,
  Ruler,
  Scale,
  Trash2,
  TriangleAlert,
  Wand2,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"

import type {
  Conn,
  Dev,
  HistEntry,
  Live,
  Op,
  Port,
  ReplayAction,
} from "@/lib/types"
import {
  AMBIENT_LEVELS,
  HOME_ACC,
  HOME_RPM,
  MAX_MM_S,
  MAX_RPM,
  MAX_UL_S,
  SYRINGE_UL,
  VALVE_MS,
  X_MAX_MM,
  Z_MAX_MM,
  clamp,
  moveTimeMs,
  plungerMs,
  sleep,
} from "@/lib/constants"
import {
  LinearTrack,
  PlungerView,
  StageView,
  ValveDiagram,
} from "@/components/diagrams"
import { Stat } from "@/components/widgets"
import { type ApiClient, ApiError, makeHttpClient } from "@/lib/api"
import { CELLS, type CellDef } from "@/lib/cells"
import { makeMockClient } from "@/lib/mockClient"

// ── Per-cell runtime state held by the Phase shell ─────────────────────────
interface CellState {
  live: Live
  conn: Record<Dev, Conn>
  busy: Record<Dev, boolean>
  diagnosed: boolean
  initialized: boolean
}

const newCellState = (): CellState => ({
  live: {
    weightG: 0,
    path: 1,
    aspPort: 1,
    dispPort: 3,
    valveConnect: 1,
    plungerUL: 0,
    stageXmm: 0,
    stageZmm: 0,
    error: null,
  },
  conn: { pump: "idle", balance: "idle", stage: "idle" },
  busy: { pump: false, balance: false, stage: false },
  diagnosed: false,
  initialized: false,
})

// Control-panel inputs, remembered per cell.
interface Inputs {
  volume: string
  pumpSpeedPct: string
  primeCycles: string
  xTarget: string
  zTarget: string
  yTarget: string
  speedPct: string
  accelPct: string
}
const newInputs = (): Inputs => ({
  volume: "100",
  pumpSpeedPct: "60",
  primeCycles: "3",
  xTarget: "261.5",
  zTarget: "234.0",
  yTarget: "150",
  speedPct: "20",
  accelPct: "50",
})

const ALL_DEVS: Dev[] = ["pump", "balance", "stage"]
const DISPENSE_CELLS = CELLS.filter((c) => c.kind === "dispense")
const WEIGH_CELL = CELLS.find((c) => c.kind === "weigh")

interface Scenario {
  id: number
  name: string
  fromId: number
  toId: number
  fromLabel: string
  toLabel: string
}

export default function App() {
  const [cells, setCells] = useState<Record<string, CellState>>(() =>
    Object.fromEntries(CELLS.map((c) => [c.id, newCellState()])),
  )
  const [selId, setSelId] = useState(CELLS[0].id)

  // One client per cell (lazy), kept across re-renders: a real HTTP client
  // bound to that cell's backend (via its proxy base) when mock is off, else
  // an in-memory mock.
  const clientsRef = useRef<Map<string, ApiClient>>(new Map())
  const clientFor = (id: string): ApiClient => {
    let cl = clientsRef.current.get(id)
    if (!cl) {
      const def = CELLS.find((c) => c.id === id)
      cl =
        def && !def.mock ? makeHttpClient(def.base ?? "") : makeMockClient()
      clientsRef.current.set(id, cl)
    }
    return cl
  }

  // Control inputs, remembered per cell.
  const [inputs, setInputs] = useState<Record<string, Inputs>>(() =>
    Object.fromEntries(CELLS.map((c) => [c.id, newInputs()])),
  )

  // Shared animation knobs (only the active cell animates at a time).
  const [stageDurMs, setStageDurMs] = useState(500)
  const [stageEase, setStageEase] = useState("ease")
  const [plungerDurMs, setPlungerDurMs] = useState(400)

  const [history, setHistory] = useState<HistEntry[]>([])
  const histId = useRef(1)
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const scenId = useRef(1)
  const [scenName, setScenName] = useState("")
  const [scenFrom, setScenFrom] = useState("")
  const [scenTo, setScenTo] = useState("")
  const [running, setRunning] = useState(false) // a scenario is replaying

  const cellsRef = useRef(cells)
  cellsRef.current = cells

  // ── state helpers ─────────────────────────────────────────────────────
  const patchCell = (id: string, fn: (c: CellState) => CellState) =>
    setCells((cs) => ({ ...cs, [id]: fn(cs[id]) }))
  const patchLive = (id: string, fn: (l: Live) => Live) =>
    patchCell(id, (c) => ({ ...c, live: fn(c.live) }))

  const pushHist = (id: string, label: string, action?: ReplayAction) => {
    const name = CELLS.find((c) => c.id === id)?.name ?? id
    const at = new Date().toLocaleTimeString()
    setHistory((h) =>
      [
        { id: histId.current++, at, label: `${name} · ${label}`, action },
        ...h,
      ].slice(0, 300),
    )
  }

  const fail = (id: string, e: unknown) => {
    const msg =
      e instanceof ApiError ? `${e.errorName}: ${e.message}` : String(e)
    patchLive(id, (l) => ({ ...l, error: msg }))
    toast.error(msg)
  }

  const setBusy = (id: string, devs: Dev[], on: boolean) =>
    patchCell(id, (c) => ({
      ...c,
      busy: { ...c.busy, ...Object.fromEntries(devs.map((d) => [d, on])) },
    }))

  const withBusy = async (id: string, devs: Dev[], fn: () => Promise<void>) => {
    setBusy(id, devs, true)
    try {
      await fn()
    } catch (e) {
      fail(id, e)
    } finally {
      setBusy(id, devs, false)
    }
  }

  // Await a real call together with a minimum animation time.
  const withAnim = async <T,>(p: Promise<T>, ms: number): Promise<T> => {
    const [r] = await Promise.all([p, sleep(ms)])
    return r
  }
  const asPort = (v: string): Port => (v === "3" ? 3 : 1)

  // ── Live readout polling for every cell (skip a cell while it's busy) ──
  useEffect(() => {
    const t = setInterval(async () => {
      for (const c of CELLS) {
        const st = cellsRef.current[c.id]
        if (st.busy.pump || st.busy.balance || st.busy.stage) continue
        try {
          const s = await clientFor(c.id).status()
          patchLive(c.id, (l) => ({
            ...l,
            weightG: s.weight_g,
            plungerUL: s.plunger_uL,
            stageXmm: s.stage_x_mm,
            stageZmm: s.stage_z_mm,
            valveConnect: asPort(s.valve),
            error: s.error,
          }))
        } catch {
          /* unreachable backend — keep last-known */
        }
      }
    }, 2000)
    return () => clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── per-cell operations (id-parameterised) ────────────────────────────
  const applyDiagnose = (
    id: string,
    d: Awaited<ReturnType<ApiClient["diagnose"]>>,
  ) =>
    patchCell(id, (c) => ({
      ...c,
      conn: {
        pump: d.pump.ok ? "ok" : "fault",
        balance: d.balance.ok ? "ok" : "fault",
        stage: d.stage.ok ? "ok" : "fault",
      },
      diagnosed: true,
      live: { ...c.live, error: null },
    }))

  const diagnoseCell = (id: string) =>
    withBusy(id, ALL_DEVS, async () => {
      pushHist(id, "Diagnose", { kind: "diagnose", cell: id })
      applyDiagnose(id, await clientFor(id).diagnose())
    })

  const initializeCell = (id: string) =>
    withBusy(id, ["pump"], async () => {
      pushHist(id, "Initialize pump", { kind: "initialize", cell: id })
      const r = await clientFor(id).initialize(2)
      patchCell(id, (c) => ({
        ...c,
        initialized: true,
        live: {
          ...c.live,
          plungerUL: r.plunger_uL,
          valveConnect: asPort(r.valve),
        },
      }))
    })

  const tareCell = (id: string) =>
    withBusy(id, ["balance"], async () => {
      pushHist(id, "Tare", { kind: "tare", cell: id })
      const r = await clientFor(id).tare()
      patchLive(id, (l) => ({ ...l, weightG: r.weight_g }))
    })

  const setAmbientCell = (id: string, level: string) =>
    withBusy(id, ["balance"], async () => {
      await clientFor(id).ambient(level)
      pushHist(id, `Ambient → ${level}`, { kind: "ambient", cell: id, level })
    })

  const doActivation = async (id: string, op: Extract<Op, { kind: "dispense" }>) => {
    const v = clamp(op.v, 0, SYRINGE_UL)
    const ms = plungerMs(v, op.pumpPct)
    const cl = clientFor(id)
    patchLive(id, (l) => ({
      ...l,
      path: op.path,
      aspPort: op.asp,
      dispPort: op.disp,
      valveConnect: op.asp,
    }))
    await withAnim(cl.valve(op.asp), VALVE_MS)
    setPlungerDurMs(ms)
    patchLive(id, (l) => ({ ...l, plungerUL: v }))
    await withAnim(cl.aspirate(v), ms)
    patchLive(id, (l) => ({ ...l, valveConnect: op.disp }))
    await withAnim(cl.valve(op.disp), VALVE_MS)
    setPlungerDurMs(ms)
    patchLive(id, (l) => ({ ...l, plungerUL: 0 }))
    await withAnim(cl.dispense(0), ms)
    toast.success(`${cellName(id)} — ${v} µL P${op.asp}→P${op.disp}`)
  }

  const doPrime = async (id: string, op: Extract<Op, { kind: "prime" }>) => {
    const ms = plungerMs(SYRINGE_UL, op.pumpPct)
    const done = clientFor(id).cycle(op.n, SYRINGE_UL, op.src, op.disp)
    for (let i = 1; i <= op.n; i++) {
      setPlungerDurMs(ms)
      patchLive(id, (l) => ({ ...l, plungerUL: SYRINGE_UL }))
      await sleep(ms)
      setPlungerDurMs(ms)
      patchLive(id, (l) => ({ ...l, plungerUL: 0 }))
      await sleep(ms)
    }
    await done
    toast.success(`${cellName(id)} — primed ×${op.n}`)
  }

  // One timed gantry segment; animation duration tracks the real move time.
  const stageSeg = async (
    dist: number,
    rpm: number,
    acc: number,
    apply: () => void,
  ) => {
    const realMs = moveTimeMs(dist, rpm, acc)
    const animMs = clamp(realMs, 150, 6000)
    setStageDurMs(animMs)
    apply()
    await sleep(animMs)
    return realMs
  }

  const doMoveStage = async (id: string, op: Extract<Op, { kind: "stage" }>) => {
    const x = clamp(op.x, 0, X_MAX_MM)
    const z = clamp(op.z, 0, Z_MAX_MM)
    const rpm = (clamp(op.sPct, 1, 100) / 100) * MAX_RPM
    // accel 0 is the MKS exception — no ramp, runs straight at set speed.
    const acc = Math.round((clamp(op.aPct, 0, 100) / 100) * 255)
    setStageEase(acc === 0 ? "linear" : "ease")
    const cur = cellsRef.current[id].live
    if (x === cur.stageXmm) {
      await stageSeg(z - cur.stageZmm, rpm, acc, () =>
        patchLive(id, (l) => ({ ...l, stageZmm: z })),
      )
    } else {
      await stageSeg(cur.stageZmm, rpm, acc, () =>
        patchLive(id, (l) => ({ ...l, stageZmm: 0 })),
      )
      await stageSeg(x - cur.stageXmm, rpm, acc, () =>
        patchLive(id, (l) => ({ ...l, stageXmm: x })),
      )
      await stageSeg(z, rpm, acc, () =>
        patchLive(id, (l) => ({ ...l, stageZmm: z })),
      )
    }
    await clientFor(id).stageMove(x, z, op.sPct, op.aPct)
    toast.success(`${cellName(id)} gantry → X ${x} / Z ${z} mm`)
  }

  const doHome = async (id: string) => {
    setStageEase("ease")
    const cur = cellsRef.current[id].live
    await stageSeg(cur.stageZmm, HOME_RPM, HOME_ACC, () =>
      patchLive(id, (l) => ({ ...l, stageZmm: 0 })),
    )
    if (cur.stageXmm !== 0)
      await stageSeg(cur.stageXmm, HOME_RPM, HOME_ACC, () =>
        patchLive(id, (l) => ({ ...l, stageXmm: 0 })),
      )
    await clientFor(id).stageHome()
    toast.success(`${cellName(id)} homed`)
  }

  // cell4 linear Y: single axis, mapped onto stageXmm.
  const doLinearMove = async (id: string, yMm: number) => {
    const inI = inputs[id]
    const y = clamp(yMm, 0, X_MAX_MM)
    const rpm = (clamp(Number(inI.speedPct) || 0, 1, 100) / 100) * MAX_RPM
    const acc = Math.round((clamp(Number(inI.accelPct) || 0, 0, 100) / 100) * 255)
    setStageEase(acc === 0 ? "linear" : "ease")
    await stageSeg(y - cellsRef.current[id].live.stageXmm, rpm, acc, () =>
      patchLive(id, (l) => ({ ...l, stageXmm: y })),
    )
    await clientFor(id).stageMove(y, 0, Number(inI.speedPct) || 20, acc)
    toast.success(`${cellName(id)} linear Y → ${y} mm`)
  }

  const cellName = (id: string) => CELLS.find((c) => c.id === id)?.name ?? id

  // ── op param snapshots from the cell's own inputs + valve config ──────
  const dispenseOp = (id: string): Extract<Op, { kind: "dispense" }> => {
    const lv = cellsRef.current[id].live
    const inI = inputs[id]
    return {
      kind: "dispense",
      v: clamp(Number(inI.volume) || 0, 0, SYRINGE_UL),
      asp: lv.aspPort,
      disp: lv.dispPort,
      path: lv.path,
      pumpPct: clamp(Number(inI.pumpSpeedPct) || 0, 1, 100),
    }
  }
  const stageOp = (id: string): Extract<Op, { kind: "stage" }> => {
    const inI = inputs[id]
    return {
      kind: "stage",
      x: clamp(Number(inI.xTarget) || 0, 0, X_MAX_MM),
      z: clamp(Number(inI.zTarget) || 0, 0, Z_MAX_MM),
      sPct: clamp(Number(inI.speedPct) || 0, 1, 100),
      aPct: clamp(Number(inI.accelPct) || 0, 0, 100),
    }
  }
  const primeOp = (id: string): Extract<Op, { kind: "prime" }> => {
    const lv = cellsRef.current[id].live
    const inI = inputs[id]
    return {
      kind: "prime",
      n: clamp(Number(inI.primeCycles) || 1, 1, 10),
      pumpPct: clamp(Number(inI.pumpSpeedPct) || 0, 1, 100),
      src: lv.aspPort,
      disp: lv.dispPort,
    }
  }

  // button wrappers act on the selected cell
  const activation = () => {
    const op = dispenseOp(selId)
    pushHist(
      selId,
      `Dispense ${op.v} µL · Path ${op.path} · P${op.asp}→P${op.disp}`,
      { kind: "dispense", cell: selId, op },
    )
    return withBusy(selId, ["pump"], () => doActivation(selId, op))
  }
  const prime = () => {
    const op = primeOp(selId)
    pushHist(selId, `Prime ×${op.n}`, { kind: "prime", cell: selId, op })
    return withBusy(selId, ["pump"], () => doPrime(selId, op))
  }
  const moveStage = () => {
    const op = stageOp(selId)
    pushHist(selId, `Gantry → X ${op.x} / Z ${op.z} mm`, {
      kind: "stage",
      cell: selId,
      op,
    })
    return withBusy(selId, ["stage"], () => doMoveStage(selId, op))
  }
  const homeStage = () => {
    pushHist(selId, "Home gantry", { kind: "home", cell: selId })
    return withBusy(selId, ["stage"], () => doHome(selId))
  }
  const linearMove = () => {
    const y = clamp(Number(inputs[selId].yTarget) || 0, 0, X_MAX_MM)
    pushHist(selId, `Linear Y → ${y} mm`, { kind: "linear", cell: selId, y })
    return withBusy(selId, ["stage"], () => doLinearMove(selId, y))
  }
  const linearHome = () => {
    pushHist(selId, "Home linear Y", { kind: "home", cell: selId })
    return withBusy(selId, ["stage"], () => doHome(selId))
  }

  const setPath = (p: 1 | 2) => patchLive(selId, (l) => ({ ...l, path: p }))
  const setAsp = (p: Port) =>
    patchLive(selId, (l) => ({ ...l, aspPort: p, dispPort: p === 1 ? 3 : 1 }))
  const setDisp = (p: Port) =>
    patchLive(selId, (l) => ({ ...l, dispPort: p, aspPort: p === 1 ? 3 : 1 }))

  // ── Phase-level lifecycle ─────────────────────────────────────────────
  const diagnoseAll = async () => {
    for (const c of CELLS) await diagnoseCell(c.id)
  }
  const setupAll = async () => {
    for (const c of CELLS) {
      await diagnoseCell(c.id)
      if (c.kind === "dispense") await initializeCell(c.id)
      else await tareCell(c.id)
    }
    toast.success("Phase setup complete")
  }
  const stopAll = () => {
    for (const c of CELLS) {
      setBusy(c.id, ALL_DEVS, false)
      patchLive(c.id, (l) => ({ ...l, error: null }))
      clientFor(c.id)
        .stop()
        .catch(() => {})
    }
    pushHist(selId, "STOP — all cells")
    setRunning(false)
    toast.warning("STOP — all motion aborted")
  }

  // ── scenario replay: re-run a saved span of History on its cells ───────
  const runAction = async (a: ReplayAction) => {
    if (a.kind === "diagnose") await diagnoseCell(a.cell)
    else if (a.kind === "initialize") await initializeCell(a.cell)
    else if (a.kind === "tare") await tareCell(a.cell)
    else if (a.kind === "ambient") await setAmbientCell(a.cell, a.level)
    else if (a.kind === "dispense")
      await withBusy(a.cell, ["pump"], () => doActivation(a.cell, a.op))
    else if (a.kind === "prime")
      await withBusy(a.cell, ["pump"], () => doPrime(a.cell, a.op))
    else if (a.kind === "stage")
      await withBusy(a.cell, ["stage"], () => doMoveStage(a.cell, a.op))
    else if (a.kind === "home")
      await withBusy(a.cell, ["stage"], () => doHome(a.cell))
    else if (a.kind === "linear")
      await withBusy(a.cell, ["stage"], () => doLinearMove(a.cell, a.y))
  }

  const runScenario = async (s: Scenario) => {
    const lo = Math.min(s.fromId, s.toId)
    const hi = Math.max(s.fromId, s.toId)
    const span = history
      .filter((h) => h.id >= lo && h.id <= hi && h.action)
      .sort((a, b) => a.id - b.id) // oldest → newest
    if (span.length === 0) {
      toast.error("Scenario has no replayable commands")
      return
    }
    setRunning(true)
    try {
      for (const h of span) {
        toast.info(`▶ ${h.label}`)
        await runAction(h.action!)
      }
      toast.success(`Scenario "${s.name}" complete`)
    } finally {
      setRunning(false)
    }
  }

  // ── derived state for the selected cell ───────────────────────────────
  const selDef = CELLS.find((c) => c.id === selId) as CellDef
  const sc = cells[selId]
  const inp = inputs[selId]
  const setInput = (k: keyof Inputs, v: string) =>
    setInputs((is) => ({ ...is, [selId]: { ...is[selId], [k]: v } }))
  const selBusy = sc.busy.pump || sc.busy.balance || sc.busy.stage
  const ready = !selBusy && !sc.live.error && !running
  const canInit = ready && sc.diagnosed
  const canDrive = ready && sc.initialized

  const cellStateWord = (st: CellState): {
    word: string
    cls: string
    fault: boolean
  } => {
    if (st.live.error)
      return { word: "fault", cls: "text-status-fault", fault: true }
    if (st.busy.pump || st.busy.balance || st.busy.stage)
      return { word: "busy", cls: "text-status-warn", fault: false }
    if (st.diagnosed) return { word: "ready", cls: "text-status-ok", fault: false }
    return { word: "—", cls: "text-status-idle", fault: false }
  }

  const histLabel = (h: HistEntry) => `${h.at} · ${h.label}`
  const saveScenario = () => {
    if (history.length === 0) return
    const fromId = scenFrom ? Number(scenFrom) : history[history.length - 1].id
    const toId = scenTo ? Number(scenTo) : history[0].id
    const lbl = (id: number) => history.find((h) => h.id === id)?.label ?? "?"
    setScenarios((s) => [
      ...s,
      {
        id: scenId.current++,
        name: scenName || `Scenario ${s.length + 1}`,
        fromId,
        toId,
        fromLabel: lbl(fromId),
        toLabel: lbl(toId),
      },
    ])
    setScenName("")
    setScenFrom("")
    setScenTo("")
    toast.success("Scenario saved")
  }

  return (
    <div className="min-h-dvh bg-background text-foreground">
      {/* ── Phase header ─────────────────────────────────────────────── */}
      <header className="flex items-center gap-4 border-b px-4 py-2">
        <h1 className="text-sm font-semibold tracking-tight">Phase 1</h1>
        <span className="text-xs text-muted-foreground">
          3-solution synthesis · {CELLS.length} cells
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" onClick={setupAll} disabled={running}>
            <Wand2 className="size-4" /> Setup all
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={diagnoseAll}
            disabled={running}
          >
            <RefreshCw className="size-4" /> Diagnose all
          </Button>
          <Dialog>
            <DialogTrigger
              render={
                <Button
                  size="sm"
                  className="bg-status-fault font-semibold text-white hover:bg-status-fault/85"
                >
                  <OctagonX className="size-4" /> STOP
                </Button>
              }
            />
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Abort all motion?</DialogTitle>
                <DialogDescription>
                  Immediately stops every cell. Use in an emergency.
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <DialogClose render={<Button variant="outline">Cancel</Button>} />
                <DialogClose
                  render={
                    <Button
                      className="bg-status-fault font-semibold text-white hover:bg-status-fault/85"
                      onClick={stopAll}
                    >
                      <OctagonX className="size-4" /> Confirm STOP
                    </Button>
                  }
                />
              </DialogFooter>
            </DialogContent>
          </Dialog>
        </div>
      </header>

      <main className="grid gap-4 p-4 lg:grid-cols-[1fr_28rem]">
        {/* ── LEFT: Phase-wide monitoring ──────────────────────────── */}
        <div className="flex flex-col gap-4">
          {/* Live — 15 readouts: {weight, valve, plunger, XZ gantry, state} × cell1–3 */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Activity className="size-4" /> Live
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="grid grid-cols-3 gap-2">
              {DISPENSE_CELLS.map((c, i) => {
                const st = cells[c.id]
                const sw = cellStateWord(st)
                return (
                  <div key={c.id} className="flex flex-col gap-2">
                    <span className="text-xs font-medium text-muted-foreground">
                      {c.name}
                    </span>
                    <Stat label={`weight ${i + 1}`} value={`${st.live.weightG.toFixed(4)} g`} />
                    <Stat
                      label={`valve ${i + 1}`}
                      value={`Path ${st.live.path} · P${st.live.aspPort}→P${st.live.dispPort}`}
                    />
                    <Stat label={`plunger ${i + 1}`} value={`${st.live.plungerUL} µL`} />
                    <Stat
                      label={`XZ gantry ${i + 1}`}
                      value={`${st.live.stageXmm.toFixed(0)} / ${st.live.stageZmm.toFixed(0)} mm`}
                    />
                    <div className="flex flex-col">
                      <span className="text-xs text-muted-foreground">{`state ${i + 1}`}</span>
                      <span className={`inline-flex items-center gap-1 font-medium ${sw.cls}`}>
                        {sw.fault && <TriangleAlert className="size-3.5" aria-hidden />}
                        {sw.word}
                      </span>
                    </div>
                  </div>
                )
              })}
              </div>
              {WEIGH_CELL &&
                (() => {
                  const st = cells[WEIGH_CELL.id]
                  const sw = cellStateWord(st)
                  return (
                    <div className="flex flex-wrap items-start gap-6 border-t pt-3">
                      <span className="text-xs font-medium text-muted-foreground">
                        {WEIGH_CELL.name}
                      </span>
                      <Stat label="weight" value={`${st.live.weightG.toFixed(4)} g`} />
                      <Stat label="linear Y" value={`${st.live.stageXmm.toFixed(0)} mm`} />
                      <div className="flex flex-col">
                        <span className="text-xs text-muted-foreground">state</span>
                        <span className={`inline-flex items-center gap-1 font-medium ${sw.cls}`}>
                          {sw.fault && <TriangleAlert className="size-3.5" aria-hidden />}
                          {sw.word}
                        </span>
                      </div>
                    </div>
                  )
                })()}
            </CardContent>
          </Card>

          {/* Visualization — 3 cell mini-views + the shared linear-motor track */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Eye className="size-4" /> Visualization
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-1">
              {/* left-aligned; cells share an 85% width grid (gap-0) with the
                  track below so the stops still sit under each gantry. */}
              <div className="grid w-[85%] grid-cols-3">
                {DISPENSE_CELLS.map((c) => {
                  const lv = cells[c.id].live
                  return (
                    <div key={c.id} className="flex flex-col items-start gap-1 pr-2">
                      <span className="text-xs font-medium text-muted-foreground">
                        {c.name}
                      </span>
                      <div className="flex items-end gap-1">
                        <ValveDiagram
                          path={lv.path}
                          connect={lv.valveConnect}
                          durMs={VALVE_MS}
                          className="h-20 w-20"
                        />
                        <PlungerView uL={lv.plungerUL} durMs={plungerDurMs} className="h-20 w-8" />
                      </div>
                      <div className="w-[49%]">
                        <StageView
                          x={lv.stageXmm}
                          z={lv.stageZmm}
                          durMs={stageDurMs}
                          ease={stageEase}
                        />
                      </div>
                    </div>
                  )
                })}
              </div>
              {WEIGH_CELL && (
                <div className="w-[85%]">
                  <LinearTrack
                    mm={cells[WEIGH_CELL.id].live.stageXmm}
                    maxMm={X_MAX_MM}
                    weightG={cells[WEIGH_CELL.id].live.weightG}
                    cells={DISPENSE_CELLS.map((c) => c.name)}
                    durMs={stageDurMs}
                    ease={stageEase}
                    tickOffset={0.25}
                  />
                </div>
              )}
            </CardContent>
          </Card>

          {/* Scenario — register a History range as a named, reusable scenario */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <ListChecks className="size-4" /> Scenario
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <span className="text-xs text-muted-foreground">
                Pick a start/end from the command History below and save the
                span as a scenario (you can register several).
              </span>
              <div className="flex flex-wrap items-end gap-2">
                <div className="flex flex-col gap-1">
                  <Label className="text-xs">From</Label>
                  <Select value={scenFrom} onValueChange={(v) => setScenFrom(v ?? "")}>
                    <SelectTrigger className="h-8 w-44">
                      <SelectValue placeholder="(oldest)" />
                    </SelectTrigger>
                    <SelectContent>
                      {history.map((h) => (
                        <SelectItem key={h.id} value={String(h.id)}>
                          {histLabel(h)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex flex-col gap-1">
                  <Label className="text-xs">To</Label>
                  <Select value={scenTo} onValueChange={(v) => setScenTo(v ?? "")}>
                    <SelectTrigger className="h-8 w-44">
                      <SelectValue placeholder="(newest)" />
                    </SelectTrigger>
                    <SelectContent>
                      {history.map((h) => (
                        <SelectItem key={h.id} value={String(h.id)}>
                          {histLabel(h)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <Input
                  value={scenName}
                  onChange={(e) => setScenName(e.target.value)}
                  placeholder="scenario name"
                  className="h-8 w-40"
                />
                <Button size="sm" onClick={saveScenario} disabled={history.length === 0}>
                  <Plus className="size-4" /> Save
                </Button>
              </div>

              {scenarios.length > 0 && (
                <ol className="flex flex-col gap-1">
                  {scenarios.map((s) => (
                    <li
                      key={s.id}
                      className="flex items-center justify-between rounded border px-2 py-1 text-sm"
                    >
                      <span className="truncate">
                        <span className="font-medium">{s.name}</span>
                        <span className="text-muted-foreground">
                          {" "}
                          — {s.fromLabel} → {s.toLabel}
                        </span>
                      </span>
                      <div className="flex shrink-0 items-center gap-1">
                        <Button
                          size="xs"
                          variant="outline"
                          onClick={() => runScenario(s)}
                          disabled={running}
                        >
                          <Play className="size-3" /> Run
                        </Button>
                        <Button
                          size="icon-xs"
                          variant="ghost"
                          onClick={() =>
                            setScenarios((ss) => ss.filter((x) => x.id !== s.id))
                          }
                        >
                          <Trash2 className="size-3" />
                        </Button>
                      </div>
                    </li>
                  ))}
                </ol>
              )}

              <Separator />
              <span className="text-xs text-muted-foreground">History (newest first)</span>
              <ol className="flex max-h-52 flex-col gap-1 overflow-auto">
                {history.length === 0 ? (
                  <li className="text-xs text-muted-foreground">no commands yet</li>
                ) : (
                  history.map((h) => (
                    <li
                      key={h.id}
                      className="flex items-center gap-2 rounded border px-2 py-1 text-sm"
                    >
                      <span className="font-mono text-xs text-muted-foreground tabular-nums">
                        {h.at}
                      </span>
                      <span>{h.label}</span>
                    </li>
                  ))
                )}
              </ol>
            </CardContent>
          </Card>
        </div>

        {/* ── RIGHT: cell switcher + selected cell's 2 device tabs ─────── */}
        <Card>
          <CardContent className="pt-6">
            {/* cell switcher */}
            <div className="mb-3 flex flex-wrap gap-1">
              {CELLS.map((c) => {
                const sw = cellStateWord(cells[c.id])
                return (
                  <button
                    key={c.id}
                    onClick={() => setSelId(c.id)}
                    className={`flex flex-col rounded px-3 py-1 text-left text-xs transition ${
                      c.id === selId
                        ? "bg-muted ring-1 ring-border"
                        : "text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    <span className="font-medium">{c.name}</span>
                    <span className={`text-[10px] ${sw.cls}`}>{sw.word}</span>
                  </button>
                )
              })}
            </div>
            <div className="mb-3 text-xs text-muted-foreground">
              {selDef.name} · {selDef.sub}
              {sc.live.error && (
                <span className="ml-2 inline-flex items-center gap-1 text-status-fault">
                  <TriangleAlert className="size-3" /> {sc.live.error}
                </span>
              )}
            </div>

            {selDef.kind === "dispense" ? (
              <Tabs defaultValue="pump">
                <TabsList className="grid w-full grid-cols-2">
                  <TabsTrigger value="pump">
                    <Beaker className="size-4" /> Pump
                  </TabsTrigger>
                  <TabsTrigger value="gantry">
                    <Move3d className="size-4" /> XZ gantry
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="pump" className="flex flex-col gap-3 pt-3">
                  <Button onClick={() => initializeCell(selId)} disabled={!canInit}>
                    Initialize
                  </Button>
                  <Separator />
                  <div className="flex flex-col gap-1">
                    <Label>Path</Label>
                    <div className="flex gap-2">
                      {[1, 2].map((p) => (
                        <Button
                          key={p}
                          size="sm"
                          variant={sc.live.path === p ? "default" : "outline"}
                          disabled={!canDrive}
                          onClick={() => setPath(p as 1 | 2)}
                        >
                          Path {p}
                        </Button>
                      ))}
                    </div>
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="flex flex-col gap-1">
                      <Label>Aspirate port</Label>
                      <div className="flex gap-2">
                        {[1, 3].map((p) => (
                          <Button
                            key={p}
                            size="sm"
                            variant={sc.live.aspPort === p ? "default" : "outline"}
                            disabled={!canDrive}
                            onClick={() => setAsp(p as Port)}
                          >
                            Port {p}
                          </Button>
                        ))}
                      </div>
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label>Dispense port</Label>
                      <div className="flex gap-2">
                        {[1, 3].map((p) => (
                          <Button
                            key={p}
                            size="sm"
                            variant={sc.live.dispPort === p ? "default" : "outline"}
                            disabled={!canDrive}
                            onClick={() => setDisp(p as Port)}
                          >
                            Port {p}
                          </Button>
                        ))}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Label htmlFor="vol" className="text-xs">
                      Volume (µL)
                    </Label>
                    <Input
                      id="vol"
                      value={inp.volume}
                      onChange={(e) => setInput("volume", e.target.value)}
                      className="h-7 w-24"
                    />
                    <span className="text-xs text-muted-foreground">0–{SYRINGE_UL}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <Label htmlFor="pspd" className="text-xs">
                      Speed (%)
                    </Label>
                    <Input
                      id="pspd"
                      value={inp.pumpSpeedPct}
                      onChange={(e) => setInput("pumpSpeedPct", e.target.value)}
                      className="h-7 w-24"
                    />
                    <span className="text-xs text-muted-foreground">
                      max {MAX_UL_S.toFixed(1)} µL/s
                    </span>
                  </div>
                  <Button onClick={activation} disabled={!canDrive}>
                    <Droplet className="size-4" /> Activation
                  </Button>
                  <Separator />
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="prime">Prime · full stroke ({SYRINGE_UL} µL)</Label>
                    <div className="flex items-center gap-2">
                      <Input
                        id="prime"
                        value={inp.primeCycles}
                        onChange={(e) => setInput("primeCycles", e.target.value)}
                        className="h-7 w-16"
                      />
                      <span className="text-xs text-muted-foreground">cycles (1–10)</span>
                      <Button size="sm" variant="outline" onClick={prime} disabled={!canDrive}>
                        <RotateCw className="size-4" /> Prime
                      </Button>
                    </div>
                  </div>
                </TabsContent>

                <TabsContent value="gantry" className="flex flex-col gap-3 pt-3">
                  <div className="flex flex-col gap-1">
                    <Button onClick={homeStage} disabled={!ready || sc.conn.stage !== "ok"}>
                      Home
                    </Button>
                    <span className="text-xs text-muted-foreground">
                      Homing speed: {((HOME_RPM * 10) / 60).toFixed(0)} mm/s (fixed)
                    </span>
                  </div>
                  <Separator />
                  <div className="flex items-end gap-2">
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="spd" className="text-xs">
                        Speed (%) · max {MAX_MM_S.toFixed(0)} mm/s
                      </Label>
                      <Input
                        id="spd"
                        value={inp.speedPct}
                        onChange={(e) => setInput("speedPct", e.target.value)}
                        className="w-24"
                      />
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="acc" className="text-xs">
                        Accel (%) · 0 = instant
                      </Label>
                      <Input
                        id="acc"
                        value={inp.accelPct}
                        onChange={(e) => setInput("accelPct", e.target.value)}
                        className="w-24"
                      />
                    </div>
                  </div>
                  <Separator />
                  <div className="flex items-end gap-2">
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="x" className="text-xs">
                        X (mm) · 0–{X_MAX_MM}
                      </Label>
                      <Input
                        id="x"
                        value={inp.xTarget}
                        onChange={(e) => setInput("xTarget", e.target.value)}
                        className="w-24"
                      />
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="z" className="text-xs">
                        Z (mm) · 0–{Z_MAX_MM}
                      </Label>
                      <Input
                        id="z"
                        value={inp.zTarget}
                        onChange={(e) => setInput("zTarget", e.target.value)}
                        className="w-24"
                      />
                    </div>
                    <Button size="sm" disabled={!ready || sc.conn.stage !== "ok"} onClick={moveStage}>
                      Move
                    </Button>
                  </div>
                  <span className="text-xs text-muted-foreground">
                    Motion order: up → X → down (never diagonal).
                  </span>
                </TabsContent>
              </Tabs>
            ) : (
              // ── weigh cell (cell4): Balance + Linear Y ──
              <Tabs defaultValue="balance">
                <TabsList className="grid w-full grid-cols-2">
                  <TabsTrigger value="balance">
                    <Scale className="size-4" /> Balance
                  </TabsTrigger>
                  <TabsTrigger value="linear">
                    <Ruler className="size-4" /> Linear Y
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="balance" className="flex flex-col gap-3 pt-3">
                  <Stat label="weight" value={`${sc.live.weightG.toFixed(4)} g`} />
                  <Button onClick={() => tareCell(selId)} disabled={!ready}>
                    Tare
                  </Button>
                  <div className="flex flex-col gap-1">
                    <Label>Ambient filter</Label>
                    <Select
                      defaultValue="very_unstable"
                      disabled={!ready}
                      onValueChange={(v) => v && setAmbientCell(selId, v)}
                    >
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {AMBIENT_LEVELS.map((lv) => (
                          <SelectItem key={lv} value={lv}>
                            {lv}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <span className="text-xs text-muted-foreground">
                      How hard the balance filters vibration before declaring a
                      stable reading.
                    </span>
                  </div>
                </TabsContent>

                <TabsContent value="linear" className="flex flex-col gap-3 pt-3">
                  <Button onClick={linearHome} disabled={!ready}>
                    Home
                  </Button>
                  <Separator />
                  <div className="flex items-end gap-2">
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="lspd" className="text-xs">
                        Speed (%) · max {MAX_MM_S.toFixed(0)} mm/s
                      </Label>
                      <Input
                        id="lspd"
                        value={inp.speedPct}
                        onChange={(e) => setInput("speedPct", e.target.value)}
                        className="w-24"
                      />
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="lacc" className="text-xs">
                        Accel (%) · 0 = instant
                      </Label>
                      <Input
                        id="lacc"
                        value={inp.accelPct}
                        onChange={(e) => setInput("accelPct", e.target.value)}
                        className="w-24"
                      />
                    </div>
                  </div>
                  <div className="flex items-end gap-2">
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="y" className="text-xs">
                        Y (mm) · 0–{X_MAX_MM}
                      </Label>
                      <Input
                        id="y"
                        value={inp.yTarget}
                        onChange={(e) => setInput("yTarget", e.target.value)}
                        className="w-24"
                      />
                    </div>
                    <Button size="sm" onClick={linearMove} disabled={!ready}>
                      Move
                    </Button>
                  </div>
                  <span className="text-xs text-muted-foreground">
                    Moves the balance under a cell to weigh its dispense.
                  </span>
                </TabsContent>
              </Tabs>
            )}
          </CardContent>
        </Card>
      </main>
    </div>
  )
}
