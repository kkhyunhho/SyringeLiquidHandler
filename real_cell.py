"""Real :class:`cell.Cell` over the live pump, balance, and XZ stage.

Pump/balance wiring mirrors the proven sequence in
``cv_mass_measurement.py``. The stage is only partly wired: ``home_stage``
drives ``xz_stage.home_and_position`` (the one motion that module exposes),
while arbitrary ``move_stage`` waits on the planned migration of
``xz_stage.py`` onto the ESP32 full ``mks_motor`` driver (tracked
separately) and raises until then.

Construct with :meth:`SyringeCell.open`; the server's ``__main__`` injects
that as the ``cell_factory``. Hardware-verified at the bench, not in CI —
CI exercises the server against ``tests/server/conftest.FakeCell``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from entris_ii import PrecisionScaleController
from sy01b import SyringePumpController

from cell import (
    AMBIENT_LEVELS,
    Cell,
    DeviceFaultError,
    InvalidArgError,
    WrongStateError,
)


@dataclass(frozen=True, slots=True)
class Config:
    """Bench wiring for the cell (loaded from slh.toml)."""

    pump_port: str = "1A86:7523"
    pump_address: int = 1
    pump_baud: int = 9600
    syringe_uL: int = 125
    pump_init_force: int = 2
    scale_port: str | None = None  # None → auto-detect by Sartorius VID
    ambient: str | None = None
    stage_enable: bool = False


class SyringeCell(Cell):
    """Composition of pump (sy01b) + balance (entris_ii) + XZ stage."""

    def __init__(
        self,
        pump: SyringePumpController,
        scale: PrecisionScaleController,
        config: Config,
    ) -> None:
        self._pump = pump
        self._scale = scale
        self._cfg = config
        self._plunger_uL = 0.0
        self._stage_x_mm = 0.0
        self._stage_z_mm = 0.0
        self._initialized = False

    @classmethod
    def open(cls, config: Config) -> SyringeCell:
        scale_port = config.scale_port or PrecisionScaleController.find_port()
        pump_cfg = SyringePumpController.Config(
            port=config.pump_port,
            address=config.pump_address,
            baud=config.pump_baud,
            syringe_uL=config.syringe_uL,
            step_mode=SyringePumpController.StepMode.NORMAL,
            reply_timeout_s=2.0,
        )
        scale = PrecisionScaleController(port=scale_port)
        scale.__enter__()  # opens the SBI link (context-manager protocol)
        pump = SyringePumpController.open(pump_cfg)
        if config.ambient is not None:
            scale.set_ambient(config.ambient)
        return cls(pump, scale, config)

    # ── Discovery ───────────────────────────────────────────────────────
    def diagnose(self) -> dict:
        report = self._pump.diagnose()
        return {
            "pump": {
                "software_version": report.software_version,
                "serial_number": report.serial_number,
                "config": report.config,
                "supply_volts": report.supply_volts,
                "valve": report.valve_position,
                "ok": report.ok_to_initialize,
            },
            "balance": {
                "model": self._scale.get_model_number(),
                "serial_number": self._scale.get_serial_number(),
                "ok": True,
            },
            "stage": {
                "enabled": self._cfg.stage_enable,
                "ok": self._cfg.stage_enable,
            },
            "ok_to_initialize": report.ok_to_initialize,
            "versions": {
                "pump": report.software_version,
                "balance": self._scale.get_model_number(),
            },
        }

    def status(self) -> dict:
        return {
            "weight_g": self.read_weight()[0],
            "valve": self._pump.query_valve_position(),
            "plunger_uL": self._plunger_uL,
            "stage_x_mm": self._stage_x_mm,
            "stage_z_mm": self._stage_z_mm,
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

    # ── Pump ────────────────────────────────────────────────────────────
    def initialize(self, *, force: int = 2, ccw: bool = False) -> dict:
        self._pump.initialize(force=force, ccw=ccw)
        self._initialized = True
        self._plunger_uL = 0.0
        return {"valve": self._pump.query_valve_position(), "plunger_uL": 0.0}

    def _require_init(self) -> None:
        if not self._initialized:
            raise WrongStateError("pump not initialized", command="initialize")

    def move_valve(self, port: int) -> str:
        self._require_init()
        self._pump.move_valve_to_port(port)
        return self._pump.query_valve_position()

    def aspirate(self, target_uL: float) -> float:
        self._require_init()
        self._pump.aspirate_uL(target_uL)
        self._plunger_uL = float(target_uL)
        return self._plunger_uL

    def dispense(self, target_uL: float = 0.0) -> float:
        self._require_init()
        self._pump.dispense_uL(target_uL)
        self._plunger_uL = float(target_uL)
        return self._plunger_uL

    def cycle(
        self,
        *,
        cycles: int,
        volume_uL: float,
        source_port: int,
        dispense_port: int,
    ) -> dict:
        self._require_init()
        for _ in range(cycles):
            self._pump.move_valve_to_port(source_port)
            self._pump.aspirate_uL(volume_uL)
            self._pump.move_valve_to_port(dispense_port)
            self._pump.dispense_uL(0)
        self._plunger_uL = 0.0
        return {
            "cycles_done": cycles,
            "final_valve": self._pump.query_valve_position(),
        }

    # ── Stage ───────────────────────────────────────────────────────────
    def home_stage(self) -> tuple[float, float]:
        if not self._cfg.stage_enable:
            raise WrongStateError("stage disabled in config")
        import xz_stage

        xz_stage.home_and_position()
        self._stage_x_mm = 0.0
        self._stage_z_mm = 0.0
        return (0.0, 0.0)

    def move_stage(
        self, x_mm: float, z_mm: float, *, speed_pct: int, accel_pct: int
    ) -> tuple[float, float]:
        # xz_stage.py only exposes home_and_position(); arbitrary moves wait
        # on the ESP32 mks_motor migration (see module docstring + draft).
        raise WrongStateError(
            "arbitrary XZ move pending xz_stage → ESP32 mks_motor migration; "
            "only /stage/home is wired"
        )

    # ── Safety / lifecycle ──────────────────────────────────────────────
    def stop(self) -> None:
        # Best-effort: halt the pump. Stage halt arrives with the migration.
        halt = getattr(self._pump, "halt", None) or getattr(self._pump, "stop", None)
        if callable(halt):
            halt()
        else:
            raise DeviceFaultError("pump exposes no halt/stop", command="stop")

    def close(self) -> None:
        for dev, closer in (
            (self._pump, "close"),
            (self._scale, "__exit__"),
        ):
            fn = getattr(dev, closer, None)
            if not callable(fn):
                continue
            try:
                fn(None, None, None) if closer == "__exit__" else fn()
            except Exception:  # noqa: BLE001 — best-effort shutdown
                print(f"warning: {type(dev).__name__}.{closer} failed", file=sys.stderr)
