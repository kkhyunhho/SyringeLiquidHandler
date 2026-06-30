"""Real cell4 (weigh cell): MINAS A6 linear rail (``lmc``) + Entris-II balance.

cell4 carries the Phase's single balance on a linear rail and shuttles it
under cell1–3 to weigh each dispense. It has **no pump**, so the pump methods
of the :class:`cell.Cell` protocol raise. The ``stage`` axis is the linear
rail: absolute Y positioning uses the driver's soft closed-loop
``move_to_mm`` (±0.1 mm). Hardware-verified at the bench, not in CI.
"""

from __future__ import annotations

from dataclasses import dataclass

from entris_ii import PrecisionScaleController

from cell import (
    AMBIENT_LEVELS,
    Cell,
    InvalidArgError,
    WrongStateError,
)
from lmc import LinearMotorController


@dataclass(frozen=True, slots=True)
class WeighConfig:
    """Bench wiring for the weigh cell (loaded from slh.toml)."""

    linear_port: str = "/dev/ttyUSB0"  # MINAS A6 over RS485 (TI USB3410)
    scale_port: str | None = None  # None → auto-detect by Sartorius VID
    ambient: str | None = None


def _no_pump() -> WrongStateError:
    return WrongStateError("weigh cell has no pump", command="pump")


class WeighCell(Cell):
    """cell4 = MINAS A6 linear rail + Entris-II balance, behind ``Cell``."""

    def __init__(
        self,
        lin: LinearMotorController,
        scale: PrecisionScaleController,
        config: WeighConfig,
    ) -> None:
        self._lin = lin
        self._scale = scale
        self._cfg = config

    @classmethod
    def open(cls, config: WeighConfig) -> WeighCell:
        scale_port = config.scale_port or PrecisionScaleController.find_port()
        lin = LinearMotorController(config.linear_port)  # opens RS485 at init
        scale = PrecisionScaleController(port=scale_port)
        scale.__enter__()  # opens the SBI link (context-manager protocol)
        if config.ambient is not None:
            scale.set_ambient(config.ambient)
        return cls(lin, scale, config)

    # ── Discovery ───────────────────────────────────────────────────────
    def diagnose(self) -> dict:
        return {
            # No pump on this cell; ok=True keeps the cell from reading faulted.
            "pump": {"present": False, "ok": True},
            "balance": {
                "model": self._scale.get_model_number(),
                "serial_number": self._scale.get_serial_number(),
                "ok": True,
            },
            "stage": {  # the "stage" axis here is the linear rail
                "model": self._lin.read_model_name(),
                "version": self._lin.read_software_version(),
                "ok": True,
            },
            "ok_to_initialize": True,
            "versions": {
                "balance": self._scale.get_model_number(),
                "linear": self._lin.read_software_version(),
            },
        }

    def status(self) -> dict:
        pos = self._lin.read_position_mm()
        return {
            "weight_g": self.read_weight()[0],
            "valve": "-",  # no valve on a weigh cell
            "plunger_uL": 0.0,
            "stage_x_mm": float(pos) if pos is not None else 0.0,
            "stage_z_mm": 0.0,
            "busy": False,
            "error": None,
        }

    # ── Balance ─────────────────────────────────────────────────────────
    def tare(self) -> float:
        self._scale.tare()
        return 0.0

    def read_weight(self) -> tuple[float, bool]:
        reading = self._scale.read_stable_weight()
        return float(reading.value), True

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

    # ── Stage = linear Y rail ───────────────────────────────────────────
    def home_stage(self) -> tuple[float, float]:
        # No discrete homing on the serial driver; the encoder origin is 0 mm.
        final = self._lin.move_to_mm(0.0)
        return (float(final) if final is not None else 0.0, 0.0)

    def move_stage(
        self, x_mm: float, z_mm: float, *, speed_pct: int, accel_pct: int
    ) -> tuple[float, float]:
        # x = absolute Y target; z unused. Speed/accel live in the driver's
        # internal move profile, so the percentages are advisory here.
        final = self._lin.move_to_mm(x_mm)
        return (float(final) if final is not None else x_mm, 0.0)

    # ── Safety / lifecycle ──────────────────────────────────────────────
    def stop(self) -> None:
        # The MINAS serial driver exposes no halt; a hard stop needs servo-off
        # (Modbus variant) — wired with the bench-level interlock later.
        pass

    def close(self) -> None:
        ser = getattr(self._lin, "ser", None)
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass
        try:
            self._scale.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
