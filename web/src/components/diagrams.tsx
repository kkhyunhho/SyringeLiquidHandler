import { useEffect, useRef, useState } from "react"

import type { Port } from "@/lib/types"
import { OK, SYRINGE_UL, WARN, X_MAX_MM, Z_MAX_MM, clamp } from "@/lib/constants"

// ── Valve rotor diagram (external naming): C = 6 o'clock, Port1 = 9,
//    Port2 = 12, Port3 = 3. The working channel C↔(connected port) is the
//    selected Path's colour; the parallel bypass joins the other two ports.
//    The connection only changes when Activation runs (asp→disp). ───────────
export function ValveDiagram({
  path,
  connect,
  durMs,
}: {
  path: 1 | 2
  connect: Port
  durMs: number
}) {
  type Pt = { x: number; y: number }
  const C: Pt = { x: 70, y: 116 } // 6 o'clock (common)
  const P: Record<number, Pt> = {
    1: { x: 24, y: 70 }, // 9 o'clock
    2: { x: 70, y: 24 }, // 12 o'clock
    3: { x: 116, y: 70 }, // 3 o'clock
  }
  // The rotor's two parallel channels are rigid; switching the C-connection
  // turns the whole rotor 90°. Accumulate degrees so it always takes the
  // shortest path (turn one way to engage Port 3, back the other for Port 1).
  const prev = useRef<Port>(connect)
  const [deg, setDeg] = useState(connect === 1 ? 0 : 90)
  useEffect(() => {
    if (prev.current !== connect) {
      setDeg((d) => d + (connect === 3 ? 90 : -90))
      prev.current = connect
    }
  }, [connect])

  const workColor = path === 1 ? OK : WARN
  const bypassColor = path === 1 ? WARN : OK
  // Canonical (connect=1) layout; the group rotation places the real state.
  // Colour by connect so the channel that ends up touching C reads as working.
  const cC1 = connect === 1 ? workColor : bypassColor // C↔Port1 channel
  const c23 = connect === 1 ? bypassColor : workColor // Port2↔Port3 channel
  const chord = (a: Pt, b: Pt, color: string) => (
    <line
      x1={a.x}
      y1={a.y}
      x2={b.x}
      y2={b.y}
      stroke={color}
      strokeWidth={5}
      strokeLinecap="round"
    />
  )
  return (
    <svg viewBox="0 0 140 140" className="h-40 w-40">
      <circle cx="70" cy="70" r="52" className="fill-muted stroke-border" />
      {/* rotor: both channels turn together, shortest path, manual-timed */}
      <g
        style={{
          transform: `rotate(${deg}deg)`,
          transformBox: "view-box",
          transformOrigin: "70px 70px",
          transition: `transform ${durMs}ms ease`,
        }}
      >
        {chord(C, P[1], cC1)}
        {chord(P[2], P[3], c23)}
      </g>
      {/* stator ports (do not rotate); highlight the C-connected one */}
      {[1, 2, 3].map((n) => {
        const p = P[n]
        const on = n === connect
        return (
          <g key={n}>
            <circle
              cx={p.x}
              cy={p.y}
              r="12"
              stroke="var(--color-border)"
              fill={on ? workColor : "var(--color-card)"}
              style={{ transition: "fill 0.2s ease" }}
            />
            <text
              x={p.x}
              y={p.y + 4}
              textAnchor="middle"
              fontSize="11"
              fill={on ? "#fff" : "var(--color-foreground)"}
            >
              {n}
            </text>
          </g>
        )
      })}
      <circle cx={C.x} cy={C.y} r="11" className="fill-background stroke-border" />
      <text x={C.x} y={C.y + 4} textAnchor="middle" fontSize="10" className="fill-foreground">
        C
      </text>
    </svg>
  )
}

// ── Plunger fill (syringe on the C port): liquid level = plungerUL. ─────────
export function PlungerView({ uL, durMs }: { uL: number; durMs: number }) {
  const frac = clamp(uL / SYRINGE_UL, 0, 1)
  const x = 22
  const w = 24
  const top = 22
  const h = 88
  const fillH = frac * h
  return (
    <svg viewBox="0 0 70 140" className="h-40 w-16">
      <rect x={x} y={top} width={w} height={h} rx="3" className="fill-card stroke-border" />
      <rect
        x={x}
        y={top + (h - fillH)}
        width={w}
        height={fillH}
        fill={OK}
        style={{ transition: `y ${durMs}ms linear, height ${durMs}ms linear` }}
      />
      <rect x={x} y={top} width={w} height={h} rx="3" fill="none" className="stroke-border" />
      {/* nozzle → C */}
      <rect x={x + w / 2 - 3} y={top + h} width="6" height="10" className="fill-border" />
      <text x={x + w / 2} y={134} textAnchor="middle" fontSize="9" className="fill-muted-foreground">
        {uL} µL
      </text>
    </svg>
  )
}

// ── XZ gantry view: two Z columns carry the X beam; a carriage rides the
//    beam. Z = vertical (down = larger), X = horizontal. `ease` is the CSS
//    timing function: "linear" for accel=0 (instant speed, constant velocity
//    per the MKS manual), "ease" for a ramped move. ─────────────────────────
export function StageView({
  x,
  z,
  durMs,
  ease = "ease",
}: {
  x: number
  z: number
  durMs: number
  ease?: string
}) {
  const W = 220
  const H = 140
  const railL = 26
  const railR = W - 26
  const top = 16
  const bot = H - 22
  const dy = (z / Z_MAX_MM) * (bot - top) // bridge vertical offset (Z)
  const dx = (x / X_MAX_MM) * (railR - railL) // X head horizontal offset
  const T = { transition: `transform ${durMs}ms ${ease}` }
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <rect x="1" y="1" width={W - 2} height={H - 2} rx="6" className="fill-muted stroke-border" />
      {/* two fixed Z columns */}
      <line x1={railL} y1={top} x2={railL} y2={bot} className="stroke-border" strokeWidth={3} />
      <line x1={railR} y1={top} x2={railR} y2={bot} className="stroke-border" strokeWidth={3} />
      {/* bridge: beam + both Z carriages + X head — all move together in Z */}
      <g style={{ transform: `translateY(${dy}px)`, ...T }}>
        <line x1={railL} y1={top} x2={railR} y2={top} stroke={OK} strokeWidth={4} />
        <rect x={railL - 5} y={top - 5} width="10" height="10" fill={OK} />
        <rect x={railR - 5} y={top - 5} width="10" height="10" fill={OK} />
        <g style={{ transform: `translateX(${dx}px)`, ...T }}>
          <circle cx={railL} cy={top} r="7" fill={WARN} />
        </g>
      </g>
      <text x={railL} y={H - 6} fontSize="9" className="fill-muted-foreground">
        X {x.toFixed(0)} / Z {z.toFixed(0)} mm — up → X → down
      </text>
    </svg>
  )
}
