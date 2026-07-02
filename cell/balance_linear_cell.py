"""Real cell4 (balance + linear rail): MINAS A6 linear rail (``lmc``) + Entris-II balance.

cell4 carries the Phase's single balance on a linear rail and shuttles it
under cell1–3 to weigh each dispense. It has **no pump**, so the pump methods
of the :class:`cell_protocol.Cell` protocol raise.

The linear (Y) rail runs on the **Modbus-RTU + Block Operation** driver
(:class:`vendor.lmc.LinearMotorControllerModbus`): absolute positioning and
homing are executed by the amp's own internal PID/homing (native position
mode), so there is no software soft-loop and no overshoot. The amp must be
booted in Modbus mode (``Pr5.37=2``, ``Pr6.28=1``, homing params
``Pr60.52–54``) with a battery-backed absolute encoder (``Pr0.15=1``) so the
origin survives power cycles — see ``CLAUDE.md`` for the one-time amp setup.
Hardware-verified at the bench, not in CI.
"""

from __future__ import annotations

from dataclasses import dataclass

from vendor.entris_ii import PrecisionScaleController

from .cell_protocol import (
    AMBIENT_LEVELS,
    Cell,
    CellTimeoutError,
    DeviceFaultError,
    InvalidArgError,
    WrongStateError,
)
from vendor.lmc import LinearMotorControllerModbus


@dataclass(frozen=True, slots=True)
class BalanceLinearConfig:
    """Bench wiring for the weigh cell (loaded from slh.toml)."""

    linear_port: str = "110A:1150"  # MINAS A6 over RS485 via Moxa UPort 1150
    scale_port: str | None = None  # None → auto-detect by Sartorius VID
    ambient: str | None = None


def _no_pump() -> WrongStateError:
    # Defensive stub (mirror of PumpGantryCell._no_balance): this cell has no
    # pump, but the `Cell` protocol requires every method, so the pump methods
    # raise this instead of crashing. The web greys them out from
    # `diagnose()` pump.present=false; this only fires on a stray call (→ 409).
    # The stage methods ARE implemented here (they drive the linear rail).
    return WrongStateError("balance+linear cell has no pump", command="pump")


def _no_gantry() -> WrongStateError:
    # Defensive stub: motion here is the linear Y rail (linear action set), not
    # an XZ gantry — the gantry methods raise so a misdirected /v1/gantry/*
    # call gets a clean 409 instead of hitting the rail.
    return WrongStateError(
        "balance+linear cell has no gantry", command="gantry"
    )


class BalanceLinearCell(Cell):
    """cell4 = MINAS A6 linear rail + Entris-II balance, behind ``Cell``."""

    def __init__(
        self,
        lin: LinearMotorControllerModbus,
        scale: PrecisionScaleController,
        config: BalanceLinearConfig,
    ) -> None:
        self._lin = lin
        self._scale = scale
        self._cfg = config
        # Last settled weight (updated by read_weight/tare). status() returns
        # this instead of doing a blocking read_stable_weight every poll — a
        # 30 s settle timeout there would hold the cell lock and starve the
        # linear. The operator refreshes it on demand via read_weight.
        self._last_weight_g = 0.0

    @classmethod
    def open(cls, config: BalanceLinearConfig) -> BalanceLinearCell:
        scale_port = config.scale_port or PrecisionScaleController.find_port()
        # Modbus-RTU client; opens the RS485 link at init (Pr5.37=2 on the amp).
        lin = LinearMotorControllerModbus(config.linear_port)
        # Clear any latched alarm from a prior run, then enable the servo so
        # Block-Op moves can run. We do NOT auto-home: with the battery-backed
        # absolute encoder (Pr0.15=1) the origin persists across power cycles,
        # so read_position_mm() is already valid; the operator homes on demand.
        lin.alarm_clear()
        lin.servo_on()
        scale = PrecisionScaleController(port=scale_port)
        scale.__enter__()  # opens the SBI link (context-manager protocol)
        if config.ambient is not None:
            scale.set_ambient(config.ambient)
        return cls(lin, scale, config)

    # ── Discovery ───────────────────────────────────────────────────────
    def diagnose(self) -> dict:
        # The Modbus driver has no model/version read (unlike the standard
        # protocol); the amp's active alarm number is the health signal instead
        # (0 = no alarm). read succeeding at all proves the RS485/Modbus link.
        err = self._lin.read_error_code()
        return {
            # No pump on this cell; ok=True keeps the cell from reading faulted.
            "pump": {"present": False, "ok": True},
            "balance": {
                "model": self._scale.get_model_number(),
                "serial_number": self._scale.get_serial_number(),
                "ok": True,
            },
            "stage": {  # the "stage" axis here is the linear (Y) rail
                "protocol": "modbus-rtu/block-op",
                "error_code": err,
                "position_mm": self._lin.read_position_mm(),
                "ok": err == 0,
            },
            "ok_to_initialize": err == 0,
            "versions": {
                "balance": self._scale.get_model_number(),
                "linear": "MINAS A6 (Modbus)",
            },
        }

    def status(self) -> dict:
        pos = self._lin.read_position_mm()
        err = self._lin.read_error_code()
        return {
            "weight_g": self._last_weight_g,  # cached; refresh via read_weight
            "valve": "-",  # no valve on a weigh cell
            "plunger_uL": 0.0,
            "stage_x_mm": float(pos) if pos is not None else 0.0,
            "stage_z_mm": 0.0,
            "busy": self._lin.is_busy(),
            "error": f"amp alarm {err}" if err else None,
        }

    # ── Balance ─────────────────────────────────────────────────────────
    def tare(self) -> float:
        # Plain zero/tare (SBI Esc T) — the routine "Tare" button. Adopts the
        # current pan load as the new zero. For commissioning use calibrate().
        self._scale.tare()
        self._last_weight_g = 0.0
        return 0.0

    def calibrate(self) -> float:
        # Internal (isoCAL) calibration: the balance's built-in weight adjusts
        # span + zero. The pan MUST be empty. This is the commissioning path
        # ("Setup all"), not routine zeroing — routine zeroing uses tare().
        # The driver forces ambient to "very_unstable" during the cycle, so
        # restore the configured ambient afterward.
        reading = self._scale.calibrate_internal_very_unstable()
        if self._cfg.ambient is not None:
            self._scale.set_ambient(self._cfg.ambient)
        self._last_weight_g = float(reading.value)
        return self._last_weight_g

    def read_weight(self) -> tuple[float, bool]:
        # Settled read through the balance's stable-weight filter (AUTO W/).
        # Flush first: the auto-push stream buffers values FIFO, so without
        # this read_stable_weight returns a STALE pre-load-change reading (the
        # old 0 g) sitting at the front of the buffer instead of the freshly
        # settled weight now on the pan. Cache it so status() needn't block.
        self._scale.flush_pending_reads()
        reading = self._scale.read_stable_weight()
        self._last_weight_g = float(reading.value)
        return self._last_weight_g, True

    def set_ambient(self, level: str) -> str:
        if level not in AMBIENT_LEVELS:
            raise InvalidArgError(
                f"level must be one of {AMBIENT_LEVELS}, got {level!r}"
            )
        self._scale.set_ambient(level)
        return level

    # ── Pump (none on a weigh cell) ─────────────────────────────────────
    def initialize(self, *, force: int = 2, ccw: bool = False) -> dict:
        raise _no_pump()

    def move_valve(self, port: int) -> str:
        raise _no_pump()

    def aspirate(self, target_uL: float) -> float:
        raise _no_pump()

    def dispense(self, target_uL: float = 0.0) -> float:
        raise _no_pump()

    def cycle(
        self,
        *,
        cycles: int,
        volume_uL: float,
        source_port: int,
        dispense_port: int,
    ) -> dict:
        raise _no_pump()

    # ── Linear rail (Y) ─────────────────────────────────────────────────
    def home_linear(self) -> float:
        # Native amp homing (Block-Op CC_HOMING, Pr60.52–54): the amp runs its
        # own homing sequence and sets the mechanical origin. With the battery
        # absolute encoder this is a one-time reference the encoder then retains
        # across power cycles, so re-homing each boot is unnecessary.
        if not self._lin.home():
            raise DeviceFaultError(
                "linear homing did not complete", command="linear/home"
            )
        return self._lin.read_position_mm()

    def move_linear(self, y_mm: float) -> float:
        # Absolute Y target in mm via native Block-Op position mode: the amp's
        # internal PID drives to target, so no software soft-loop and no
        # overshoot. A None return means the move didn't finish before timeout.
        final = self._lin.move_to_mm(y_mm)
        if final is None:
            raise CellTimeoutError(
                f"linear move to {y_mm} mm did not complete",
                command="linear/move",
            )
        return float(final)

    # ── Gantry (none on a balance+linear cell) ──────────────────────────
    def home_gantry(self) -> tuple[float, float]:
        raise _no_gantry()

    def move_gantry(
        self, x_mm: float, z_mm: float, *, speed_pct: int, accel_pct: int
    ) -> tuple[float, float]:
        raise _no_gantry()

    # ── Safety / lifecycle ──────────────────────────────────────────────
    def stop(self) -> None:
        # Hard stop: drop the servo (Modbus coil SRV-ON off) so the amp cuts
        # torque immediately, aborting any in-progress Block-Op move.
        try:
            self._lin.servo_off()
        except Exception:  # noqa: BLE001 — stop must never raise
            pass

    def close(self) -> None:
        # Release the servo, then close the Modbus serial link.
        try:
            self._lin.servo_off()
        except Exception:  # noqa: BLE001 — best-effort shutdown
            pass
        ser = getattr(getattr(self._lin, "client", None), "serial", None)
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._scale.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
