import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  TriangleAlert,
  OctagonX,
  Beaker,
  Scale,
  Move3d,
  RefreshCw,
  Wand2,
  RotateCw,
  Droplet,
  Plus,
  Play,
  Trash2,
  ListChecks,
  History as HistoryIcon,
  Activity,
  Eye,
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

import type { Conn, Dev, HistEntry, Live, Op, Port, Step } from "@/lib/types"
import {
  AMBIENT_LEVELS,
  HOME_ACC,
  HOME_MM_S,
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
import { PlungerView, StageView, ValveDiagram } from "@/components/diagrams"
import { Stat, StatusPill } from "@/components/widgets"
import { api, ApiError, type ApiClient } from "@/lib/api"
import { CELLS, type CellDef } from "@/lib/cells"
import { makeMockClient } from "@/lib/mockClient"


function CellDashboard({ cell, client }: { cell: CellDef; client: ApiClient }) {
  const [diagnosed, setDiagnosed] = useState(false)
  const [initialized, setInitialized] = useState(false)
  const [conn, setConn] = useState<Record<string, Conn>>({
    pump: "idle",
    balance: "idle",
    stage: "idle",
  })
  const [live, setLive] = useState<Live>({
    weightG: 0,
    path: 1,
    aspPort: 1,
    dispPort: 3,
    valveConnect: 1,
    plungerUL: 0,
    stageXmm: 0,
    stageZmm: 0,
    error: null,
  })
  // Busy is tracked per device — only the devices an operation actually
  // occupies show busy (e.g. Prime busies the pump only).
  const [busy, setBusy] = useState<Record<Dev, boolean>>({
    pump: false,
    balance: false,
    stage: false,
  })
  const [running, setRunning] = useState(false) // a scenario is in progress
  const [volume, setVolume] = useState("100")
  const [pumpSpeedPct, setPumpSpeedPct] = useState("60")
  const [primeCycles, setPrimeCycles] = useState("3")
  const [xTarget, setXTarget] = useState("261.5")
  const [zTarget, setZTarget] = useState("234.0")
  const [speedPct, setSpeedPct] = useState("20")
  const [accelPct, setAccelPct] = useState("50")
  const [stageDurMs, setStageDurMs] = useState(500)
  const [stageEase, setStageEase] = useState("ease") // "linear" when accel=0
  const [plungerDurMs, setPlungerDurMs] = useState(400)
  const [steps, setSteps] = useState<Step[]>([])
  const [stepId, setStepId] = useState(1)
  const [history, setHistory] = useState<HistEntry[]>([])
  const histId = useRef(1)
  const pushHist = (label: string) => {
    const at = new Date().toLocaleTimeString()
    setHistory((h) => [{ id: histId.current++, at, label }, ...h].slice(0, 200))
  }

  const anyBusy = busy.pump || busy.balance || busy.stage
  const ready = !anyBusy && !running && !live.error
  const canInit = ready && diagnosed
  const canDrive = ready && initialized

  // Per-device state so a non-ready cell shows *which* device is the problem,
  // and only the device(s) actually working report busy.
  const devState = (dev: Dev) => {
    if (live.error) return { word: live.error, cls: "text-status-fault", fault: true }
    if (conn[dev] === "fault")
      return { word: "fault", cls: "text-status-fault", fault: true }
    if (conn[dev] !== "ok") return { word: "—", cls: "text-status-idle", fault: false }
    return busy[dev]
      ? { word: "busy", cls: "text-status-warn", fault: false }
      : { word: "ready", cls: "text-status-ok", fault: false }
  }

  // Always-current snapshots for routines/poller that run across awaits
  // (read the latest values, not a stale closure).
  const liveRef = useRef(live)
  liveRef.current = live
  const pollGateRef = useRef(false)
  pollGateRef.current = anyBusy || running

  // Surface a server/network error to the user and the Live state. Setting
  // live.error blocks further commands (ready=false) until a re-diagnose /
  // setup / STOP clears it.
  const fail = (e: unknown) => {
    const msg =
      e instanceof ApiError ? `${e.errorName}: ${e.message}` : String(e)
    setLive((l) => ({ ...l, error: msg }))
    toast.error(msg)
  }

  // Await a real /v1 call together with a minimum animation time, so the
  // Visualization still plays against the instant FakeCell and tracks real
  // device time on hardware (whichever is longer wins).
  const withAnim = async <T,>(p: Promise<T>, animMs: number): Promise<T> => {
    const [r] = await Promise.all([p, sleep(animMs)])
    return r
  }

  // Server valve label ("1".."4"/"?") → the UI's usable Port (1 or 3).
  const asPort = (v: string): Port => (v === "3" ? 3 : 1)

  // Run `fn` with the named devices marked busy for its duration; any error
  // is surfaced (not swallowed) and the devices freed.
  async function withBusy(devs: Dev[], fn: () => Promise<void>) {
    setBusy((b) => ({ ...b, ...Object.fromEntries(devs.map((d) => [d, true])) }))
    try {
      await fn()
    } catch (e) {
      fail(e)
    } finally {
      setBusy((b) => ({ ...b, ...Object.fromEntries(devs.map((d) => [d, false])) }))
    }
  }

  // ── Live readout polling: keep weight/plunger/stage fresh from the server
  //    while idle. Skips while busy/running so it never clobbers an
  //    in-flight animation. Poll failures (server down) are ignored. ────────
  useEffect(() => {
    const id = setInterval(async () => {
      if (pollGateRef.current) return
      try {
        const s = await client.status()
        setLive((l) => ({
          ...l,
          weightG: s.weight_g,
          plungerUL: s.plunger_uL,
          stageXmm: s.stage_x_mm,
          stageZmm: s.stage_z_mm,
          valveConnect: asPort(s.valve),
          error: s.error,
        }))
      } catch {
        /* server not reachable — leave last-known values */
      }
    }, 2000)
    return () => clearInterval(id)
  }, [])

  const ALL_DEVS: Dev[] = ["pump", "balance", "stage"]

  const applyDiagnose = (d: Awaited<ReturnType<typeof client.diagnose>>) => {
    setConn({
      pump: d.pump.ok ? "ok" : "fault",
      balance: d.balance.ok ? "ok" : "fault",
      stage: d.stage.ok ? "ok" : "fault",
    })
    setDiagnosed(true)
    setLive((l) => ({ ...l, error: null }))
  }

  const diagnose = () =>
    withBusy(ALL_DEVS, async () => {
      pushHist("Diagnose")
      applyDiagnose(await client.diagnose())
      toast.success("Diagnose OK")
    })

  const setupAll = () =>
    withBusy(ALL_DEVS, async () => {
      pushHist("Setup (diagnose + initialize + tare)")
      applyDiagnose(await client.diagnose())
      const init = await client.initialize(2)
      setInitialized(true)
      const t = await client.tare()
      setLive((l) => ({
        ...l,
        plungerUL: init.plunger_uL,
        valveConnect: asPort(init.valve),
        weightG: t.weight_g,
        error: null,
      }))
      toast.success("Setup complete — diagnosed, initialized, tared")
    })

  const initialize = () =>
    withBusy(["pump"], async () => {
      pushHist("Initialize pump")
      const r = await client.initialize(2)
      setInitialized(true)
      setLive((l) => ({
        ...l,
        plungerUL: r.plunger_uL,
        valveConnect: asPort(r.valve),
      }))
      toast.success("Pump initialized")
    })

  // Core operations (no busy toggle) — shared by buttons and the scenario
  // runner so both animate the Visualization identically.
  const doTare = async () => {
    const r = await client.tare()
    setLive((l) => ({ ...l, weightG: r.weight_g }))
    toast.success("Balance tared")
  }
  const tare = () => {
    pushHist("Tare")
    return withBusy(["balance"], doTare)
  }

  const setPath = (p: 1 | 2) => setLive((l) => ({ ...l, path: p }))
  // asp and disp ports must differ (only Port 1 / Port 3 are usable)
  const setAsp = (p: Port) =>
    setLive((l) => ({ ...l, aspPort: p, dispPort: p === 1 ? 3 : 1 }))
  const setDisp = (p: Port) =>
    setLive((l) => ({ ...l, dispPort: p, aspPort: p === 1 ? 3 : 1 }))

  // #5 Activation: valve rotor → asp port (C joins it) + aspirate, then rotor
  // → disp port + dispense. Valve turn = VALVE_MS; plunger fill time from the
  // pump's top-speed setting. Drives the Live state, so the Visualization (and
  // the scenario runner, which calls this same routine) animate together.
  const doActivation = async (op: Extract<Op, { kind: "dispense" }>) => {
    const v = clamp(op.v, 0, SYRINGE_UL)
    const ms = plungerMs(v, op.pumpPct)
    setLive((l) => ({
      ...l,
      path: op.path,
      aspPort: op.asp,
      dispPort: op.disp,
      valveConnect: op.asp,
    }))
    toast.info(`Valve → Port ${op.asp} · aspirate ${v} µL (Path ${op.path})`)
    await withAnim(client.valve(op.asp), VALVE_MS)
    setPlungerDurMs(ms)
    setLive((l) => ({ ...l, plungerUL: v }))
    await withAnim(client.aspirate(v), ms)
    setLive((l) => ({ ...l, valveConnect: op.disp }))
    toast.info(`Valve → Port ${op.disp} · dispense`)
    await withAnim(client.valve(op.disp), VALVE_MS)
    setPlungerDurMs(ms)
    setLive((l) => ({ ...l, plungerUL: 0 }))
    await withAnim(client.dispense(0), ms)
    toast.success(`Done — ${v} µL P${op.asp}→P${op.disp}`)
  }

  // Prime: full-stroke fill/empty cycles, always at max volume. The server
  // runs the whole repeated cycle in one /pump/cycle call; the client
  // animates n fill/empty cycles alongside it.
  const doPrime = async (op: Extract<Op, { kind: "prime" }>) => {
    const ms = plungerMs(SYRINGE_UL, op.pumpPct)
    const serverDone = client.cycle(op.n, SYRINGE_UL, op.src, op.disp)
    for (let i = 1; i <= op.n; i++) {
      setPlungerDurMs(ms)
      setLive((l) => ({ ...l, plungerUL: SYRINGE_UL }))
      await sleep(ms)
      setPlungerDurMs(ms)
      setLive((l) => ({ ...l, plungerUL: 0 }))
      await sleep(ms)
      toast.info(`Prime ${i}/${op.n} (${SYRINGE_UL} µL)`)
    }
    await serverDone // surface any server error + wait for real completion
    toast.success(`Primed (${op.n} cycles @ ${SYRINGE_UL} µL)`)
  }

  // One timed stage segment: animation duration tracks the real move time from
  // the speed/accel profile (MKS §9.1 + ball-screw lead), so changing speed is
  // reflected in how fast the gantry animates.
  const stageSeg = async (
    dist: number,
    rpm: number,
    acc: number,
    applyFn: () => void,
  ) => {
    const realMs = moveTimeMs(dist, rpm, acc)
    const animMs = clamp(realMs, 150, 6000)
    setStageDurMs(animMs)
    applyFn()
    await sleep(animMs)
    return realMs
  }

  // #3 motion priority: up → X → down (never diagonal). If X is unchanged,
  // move Z straight to target (no raise).
  const doMoveStage = async (op: Extract<Op, { kind: "stage" }>) => {
    const x = clamp(op.x, 0, X_MAX_MM)
    const z = clamp(op.z, 0, Z_MAX_MM)
    const rpm = (clamp(op.sPct, 1, 100) / 100) * MAX_RPM
    // accel: 0 is the MKS exception — no ramp, runs straight at set speed
    // (instant). 1–100% maps to ramp code 1–255 (slow→fast).
    const acc = Math.round((clamp(op.aPct, 0, 100) / 100) * 255)
    setStageEase(acc === 0 ? "linear" : "ease")
    const curX = liveRef.current.stageXmm
    const curZ = liveRef.current.stageZmm
    let total = 0
    if (x === curX) {
      total += await stageSeg(z - curZ, rpm, acc, () =>
        setLive((l) => ({ ...l, stageZmm: z })),
      )
    } else {
      total += await stageSeg(curZ, rpm, acc, () =>
        setLive((l) => ({ ...l, stageZmm: 0 })),
      ) // up
      total += await stageSeg(x - curX, rpm, acc, () =>
        setLive((l) => ({ ...l, stageXmm: x })),
      ) // X
      total += await stageSeg(z, rpm, acc, () =>
        setLive((l) => ({ ...l, stageZmm: z })),
      ) // down
    }
    const r = await client.stageMove(x, z, op.sPct, op.aPct)
    setLive((l) => ({ ...l, stageXmm: r.x_mm, stageZmm: r.z_mm }))
    toast.success(`Stage → X ${x} / Z ${z} mm  (≈ ${(total / 1000).toFixed(1)} s)`)
  }

  // Home: vertical move first (Z → 0, up), then X → 0, at the fixed homing
  // speed — animated like a normal move (not a snap).
  const doHome = async () => {
    setStageEase("ease") // homing accel is fixed > 0
    const curX = liveRef.current.stageXmm
    const curZ = liveRef.current.stageZmm
    let total = 0
    total += await stageSeg(curZ, HOME_RPM, HOME_ACC, () =>
      setLive((l) => ({ ...l, stageZmm: 0 })),
    )
    if (curX !== 0)
      total += await stageSeg(curX, HOME_RPM, HOME_ACC, () =>
        setLive((l) => ({ ...l, stageXmm: 0 })),
      )
    const r = await client.stageHome()
    setLive((l) => ({ ...l, stageXmm: r.x_mm, stageZmm: r.z_mm }))
    toast.success(`Stage homed  (≈ ${(total / 1000).toFixed(1)} s)`)
  }

  const runStep = async (op: Op) => {
    if (op.kind === "dispense") await doActivation(op)
    else if (op.kind === "prime") await doPrime(op)
    else if (op.kind === "stage") await doMoveStage(op)
    else await doTare()
  }
  const stepDevs = (op: Op): Dev[] =>
    op.kind === "stage" ? ["stage"] : op.kind === "tare" ? ["balance"] : ["pump"]

  // ── Param snapshots from the current right-tab config ─────────────────────
  const dispenseOp = (): Extract<Op, { kind: "dispense" }> => ({
    kind: "dispense",
    v: clamp(Number(volume) || 0, 0, SYRINGE_UL),
    asp: live.aspPort,
    disp: live.dispPort,
    path: live.path,
    pumpPct: clamp(Number(pumpSpeedPct) || 0, 1, 100),
  })
  const stageOp = (): Extract<Op, { kind: "stage" }> => ({
    kind: "stage",
    x: clamp(Number(xTarget) || 0, 0, X_MAX_MM),
    z: clamp(Number(zTarget) || 0, 0, Z_MAX_MM),
    sPct: clamp(Number(speedPct) || 0, 1, 100),
    aPct: clamp(Number(accelPct) || 0, 0, 100),
  })
  const primeOp = (): Extract<Op, { kind: "prime" }> => ({
    kind: "prime",
    n: clamp(Number(primeCycles) || 1, 1, 10),
    pumpPct: clamp(Number(pumpSpeedPct) || 0, 1, 100),
    src: live.aspPort,
    disp: live.dispPort,
  })

  const activation = () => {
    const op = dispenseOp()
    pushHist(`Dispense ${op.v} µL · Path ${op.path} · P${op.asp}→P${op.disp}`)
    return withBusy(["pump"], () => doActivation(op))
  }
  const prime = () => {
    const op = primeOp()
    pushHist(`Prime ×${op.n} (${SYRINGE_UL} µL)`)
    return withBusy(["pump"], () => doPrime(op))
  }
  const homeStage = () => {
    pushHist("Home stage")
    return withBusy(["stage"], doHome)
  }
  const moveStage = () => {
    const op = stageOp()
    pushHist(`Stage → X ${op.x} / Z ${op.z} mm`)
    return withBusy(["stage"], () => doMoveStage(op))
  }

  function stopAll() {
    setBusy({ pump: false, balance: false, stage: false })
    setRunning(false)
    pushHist("STOP (abort all motion)")
    // Fire-and-forget the server abort; clear any latched local error so the
    // operator can drive again after dealing with the cause.
    client.stop().catch(() => {})
    setLive((l) => ({ ...l, error: null }))
    toast.warning("STOP — all motion aborted")
  }

  // ── Scenario / macro builder (snapshots the current right-tab config) ─────
  const addStep = (label: string, op: Op) => {
    setSteps((s) => [...s, { id: stepId, label, op }])
    setStepId((n) => n + 1)
  }
  const runScenario = async () => {
    setRunning(true)
    pushHist(`Run scenario (${steps.length} steps)`)
    try {
      for (const s of steps) {
        toast.info(`▶ ${s.label}`)
        // Mark only the device this step uses busy (e.g. a prime step → pump).
        await withBusy(stepDevs(s.op), () => runStep(s.op))
        if (liveRef.current.error) {
          toast.error("Scenario stopped — a step failed")
          return
        }
      }
      toast.success("Scenario complete")
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="min-h-dvh bg-background text-foreground">
      <header className="flex items-center gap-4 border-b px-4 py-2">
        <h1 className="flex items-baseline gap-2 text-sm font-semibold tracking-tight">
          {cell.name}
          <span className="text-xs font-normal text-muted-foreground">
            {cell.sub}
          </span>
          {cell.mock && (
            <span className="rounded bg-status-warn/15 px-1.5 text-[10px] font-medium text-status-warn">
              MOCK
            </span>
          )}
        </h1>
        <div className="flex items-center gap-3">
          <StatusPill label="pump" state={conn.pump} />
          <StatusPill label="balance" state={conn.balance} />
          <StatusPill label="stage" state={conn.stage} />
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" onClick={setupAll} disabled={anyBusy || running}>
            <Wand2 className="size-4" /> Setup
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={diagnose}
            disabled={anyBusy || running}
          >
            <RefreshCw className="size-4" /> Diagnose
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
                  Immediately stops the pump and stage. Use in an emergency.
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
        {/* ── Left: live + visualization + scenario ─────────────────── */}
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Activity className="size-4" /> Live
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap items-start gap-8">
              <Stat label="weight" value={`${live.weightG.toFixed(4)} g`} />
              <Stat
                label="valve"
                value={`Path ${live.path} · P${live.aspPort}→P${live.dispPort}`}
              />
              <Stat label="plunger" value={`${live.plungerUL} µL`} />
              <Stat label="XZ gantry" value={`${live.stageXmm} / ${live.stageZmm} mm`} />
              <div className="flex flex-col gap-1">
                <span className="text-xs text-muted-foreground">state</span>
                <div className="flex gap-4">
                  {(["pump", "balance", "stage"] as const).map((d) => {
                    const s = devState(d)
                    return (
                      <span key={d} className="flex flex-col">
                        <span className="text-xs text-muted-foreground">{d}</span>
                        <span className={`inline-flex items-center gap-1 font-medium ${s.cls}`}>
                          {s.fault && <TriangleAlert className="size-3.5" aria-hidden />}
                          {s.word}
                        </span>
                      </span>
                    )
                  })}
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Eye className="size-4" /> Visualization
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap items-center justify-start gap-10">
              <div className="flex items-end gap-3">
                <div className="flex flex-col items-center gap-1">
                  <ValveDiagram
                    path={live.path}
                    connect={live.valveConnect}
                    durMs={VALVE_MS}
                  />
                  <span className="text-xs text-muted-foreground">Valve</span>
                </div>
                <div className="flex flex-col items-center gap-1">
                  <PlungerView uL={live.plungerUL} durMs={plungerDurMs} />
                  <span className="text-xs text-muted-foreground">Plunger</span>
                </div>
              </div>
              <div className="flex flex-col items-center gap-1">
                <div className="w-full max-w-[200px]">
                  <StageView
                    x={live.stageXmm}
                    z={live.stageZmm}
                    durMs={stageDurMs}
                    ease={stageEase}
                  />
                </div>
                <span className="text-xs text-muted-foreground">
                  XZ gantry
                </span>
              </div>
            </CardContent>
          </Card>

          {/* #1 Scenario / macro */}
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <ListChecks className="size-4" /> Scenario
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <div className="flex flex-wrap gap-2">
                <Button
                  size="xs"
                  variant="outline"
                  onClick={() => {
                    const o = dispenseOp()
                    addStep(
                      `Dispense ${o.v} µL · Path ${o.path} · P${o.asp}→P${o.disp}`,
                      o,
                    )
                  }}
                >
                  <Plus className="size-3" /> Dispense
                </Button>
                <Button
                  size="xs"
                  variant="outline"
                  onClick={() => {
                    const o = stageOp()
                    addStep(`Stage → X ${o.x} / Z ${o.z} mm`, o)
                  }}
                >
                  <Plus className="size-3" /> Stage move
                </Button>
                <Button
                  size="xs"
                  variant="outline"
                  onClick={() => {
                    const o = primeOp()
                    addStep(`Prime ×${o.n} (${SYRINGE_UL} µL)`, o)
                  }}
                >
                  <Plus className="size-3" /> Prime
                </Button>
                <Button
                  size="xs"
                  variant="outline"
                  onClick={() => addStep("Tare", { kind: "tare" })}
                >
                  <Plus className="size-3" /> Tare
                </Button>
              </div>
              <ol className="flex flex-col gap-1">
                {steps.length === 0 ? (
                  <li className="text-xs text-muted-foreground">
                    no steps — add from the current configuration above
                  </li>
                ) : (
                  steps.map((s, i) => (
                    <li
                      key={s.id}
                      className="flex items-center justify-between rounded border px-2 py-1 text-sm"
                    >
                      <span>
                        <span className="text-muted-foreground">{i + 1}.</span> {s.label}
                      </span>
                      <Button
                        size="icon-xs"
                        variant="ghost"
                        onClick={() => setSteps((st) => st.filter((x) => x.id !== s.id))}
                      >
                        <Trash2 className="size-3" />
                      </Button>
                    </li>
                  ))
                )}
              </ol>
              <div className="flex gap-2">
                <Button
                  size="sm"
                  onClick={runScenario}
                  disabled={!canDrive || steps.length === 0}
                >
                  <Play className="size-4" /> Run scenario
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setSteps([])}
                  disabled={steps.length === 0}
                >
                  Clear
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* ── Right: device control tabs + history ──────────────────── */}
        <div className="flex flex-col gap-4">
        <Card>
          <CardContent className="pt-6">
            <Tabs defaultValue="pump">
              <TabsList className="grid w-full grid-cols-3">
                <TabsTrigger value="balance">
                  <Scale className="size-4" /> Balance
                </TabsTrigger>
                <TabsTrigger value="pump">
                  <Beaker className="size-4" /> Pump
                </TabsTrigger>
                <TabsTrigger value="stage">
                  <Move3d className="size-4" /> XZ gantry
                </TabsTrigger>
              </TabsList>

              <TabsContent value="balance" className="flex flex-col gap-3 pt-3">
                <Button onClick={tare} disabled={!ready || conn.balance !== "ok"}>
                  Tare
                </Button>
                <div className="flex flex-col gap-1">
                  <Label>Ambient filter</Label>
                  <Select defaultValue="very_unstable" disabled={!ready}>
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
                    stable reading. Looser (unstable / very_unstable) settles
                    faster in a noisy setup; tighter (stable) is more accurate
                    on a steady bench.
                  </span>
                </div>
              </TabsContent>

              <TabsContent value="pump" className="flex flex-col gap-3 pt-3">
                <Button onClick={initialize} disabled={!canInit}>
                  Initialize
                </Button>

                <Separator />
                {/* #5 Valve routing: Path + asp/disp ports (Port 1 / 3 only) */}
                <div className="flex flex-col gap-1">
                  <Label>Path</Label>
                  <div className="flex gap-2">
                    {[1, 2].map((p) => (
                      <Button
                        key={p}
                        size="sm"
                        variant={live.path === p ? "default" : "outline"}
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
                          variant={live.aspPort === p ? "default" : "outline"}
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
                          variant={live.dispPort === p ? "default" : "outline"}
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
                    value={volume}
                    onChange={(e) => setVolume(e.target.value)}
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
                    value={pumpSpeedPct}
                    onChange={(e) => setPumpSpeedPct(e.target.value)}
                    className="h-7 w-24"
                  />
                  <span className="text-xs text-muted-foreground">
                    max {MAX_UL_S.toFixed(1)} µL/s
                  </span>
                </div>
                <Button onClick={activation} disabled={!canDrive}>
                  <Droplet className="size-4" /> Activation
                </Button>
                {!canDrive && (
                  <span className="inline-flex items-center gap-1 text-xs text-status-warn">
                    <TriangleAlert className="size-3" aria-hidden />
                    requires Initialize + ready
                  </span>
                )}

                <Separator />
                <div className="flex flex-col gap-1">
                  <Label htmlFor="prime">Prime · full stroke ({SYRINGE_UL} µL)</Label>
                  <div className="flex items-center gap-2">
                    <Input
                      id="prime"
                      value={primeCycles}
                      onChange={(e) => setPrimeCycles(e.target.value)}
                      className="h-7 w-16"
                    />
                    <span className="text-xs text-muted-foreground">cycles (1–10)</span>
                    <Button size="sm" variant="outline" onClick={prime} disabled={!canDrive}>
                      <RotateCw className="size-4" /> Prime
                    </Button>
                  </div>
                </div>
              </TabsContent>

              <TabsContent value="stage" className="flex flex-col gap-3 pt-3">
                <div className="flex flex-col gap-1">
                  <Button onClick={homeStage} disabled={!ready || conn.stage !== "ok"}>
                    Home
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    Homing speed: {HOME_MM_S.toFixed(0)} mm/s (fixed) · Z up → X
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
                      value={speedPct}
                      onChange={(e) => setSpeedPct(e.target.value)}
                      className="w-24"
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="acc" className="text-xs">
                      Accel (%) · 0 = instant
                    </Label>
                    <Input
                      id="acc"
                      value={accelPct}
                      onChange={(e) => setAccelPct(e.target.value)}
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
                      value={xTarget}
                      onChange={(e) => setXTarget(e.target.value)}
                      className="w-24"
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="z" className="text-xs">
                      Z (mm) · 0–{Z_MAX_MM}
                    </Label>
                    <Input
                      id="z"
                      value={zTarget}
                      onChange={(e) => setZTarget(e.target.value)}
                      className="w-24"
                    />
                  </div>
                  <Button size="sm" disabled={!ready || conn.stage !== "ok"} onClick={moveStage}>
                    Move
                  </Button>
                </div>
                <span className="text-xs text-muted-foreground">
                  Motion order: up → X → down (never diagonal).
                </span>
              </TabsContent>
            </Tabs>
          </CardContent>
        </Card>

        {/* History — issued commands, kept beside the controls that make them */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <HistoryIcon className="size-4" /> History
            </CardTitle>
            <Button
              size="xs"
              variant="outline"
              onClick={() => setHistory([])}
              disabled={history.length === 0}
            >
              <Trash2 className="size-3" /> Clear
            </Button>
          </CardHeader>
          <CardContent>
            <ol className="flex max-h-72 flex-col gap-1 overflow-auto">
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
      </main>
    </div>
  )
}

// ── Phase shell: one web for the whole Phase. A cell selector across the top
//    switches which cell's dashboard is shown. Each cell gets its own client
//    — a per-cell in-memory mock for now (cell hardware/backends not wired);
//    flip a cell to mock:false in lib/cells.ts to use the real /v1 backend.
export default function App() {
  const [selId, setSelId] = useState(CELLS[0].id)
  const clientsRef = useRef<Map<string, ApiClient>>(new Map())
  const sel = CELLS.find((c) => c.id === selId) ?? CELLS[0]

  const clientFor = (c: CellDef): ApiClient => {
    if (!c.mock) return api
    let cl = clientsRef.current.get(c.id)
    if (!cl) {
      cl = makeMockClient()
      clientsRef.current.set(c.id, cl)
    }
    return cl
  }

  return (
    <div className="min-h-dvh bg-background text-foreground">
      <nav className="flex items-center gap-2 overflow-x-auto border-b bg-muted/40 px-4 py-1.5">
        <span className="shrink-0 text-sm font-semibold tracking-tight">Phase 1</span>
        <span className="shrink-0 text-muted-foreground">·</span>
        {CELLS.map((c) => (
          <button
            key={c.id}
            onClick={() => setSelId(c.id)}
            className={`shrink-0 rounded px-3 py-1 text-left text-xs transition ${
              c.id === selId
                ? "bg-background shadow-sm ring-1 ring-border"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <span className="block font-medium">
              {c.name}
              {c.mock && <span className="ml-1 text-status-warn">●</span>}
            </span>
            <span className="block text-[10px] text-muted-foreground">{c.sub}</span>
          </button>
        ))}
      </nav>
      {/* key={sel.id} remounts the dashboard per cell so its UI state is
          isolated; the mock client (device state) persists in clientsRef. */}
      <CellDashboard key={sel.id} cell={sel} client={clientFor(sel)} />
    </div>
  )
}
