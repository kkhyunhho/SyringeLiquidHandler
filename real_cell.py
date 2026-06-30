"""Real dispense cell (cell1): pump (``sy01b``) + XZ gantry (ESP32 ``mks_motor``).

A dispensing cell has **no balance** — the Phase's single balance lives on
cell4 (see ``weigh_cell.py``), so the balance methods of the :class:`cell.Cell`
protocol raise here. The XZ gantry is the three MKS SERVO57D motors driven by
the **full ESP32 ``mks_motor``** driver (paired-Z safety interlock, pyftdi),
addressed by FTDI serial — not the standalone used by the legacy
``xz_stage.py``.

Pump calls mirror the proven sequence in ``cv_mass_measurement.py``; gantry
calls mirror ``ESP32S3BOX3MotorController/bridge.py`` (``open_xz`` +
``move_sync`` + ``home_xz``). Motion order is up → X → down (never diagonal).
Hardware-verified at the bench, not in CI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from mks_motor import MKSMotor
from sy01b import SyringePumpController

from cell import Cell, DeviceFaultError, WrongStateError


@dataclass(frozen=True, slots=True)
class Config:
    """Bench wiring for a dispense cell (loaded from slh.toml)."""

    pump_port: str = "1A86:7523"
    pump_address: int = 1
    pump_baud: int = 9600
    syringe_uL: int = 125
    pump_init_force: int = 2
    # XZ gantry: FTDI serial of the X adapter; the other two adapters are the
    # paired Z (order doesn't matter — they always move together).
    motor_serial_x: str = "NTAM63XD"
    z_coord_invert: bool = True
    home_dir_z: int = 0x00
    home_dir_x: int = 0x00


def _no_balance() -> WrongStateError:
    return WrongStateError("dispense cell has no balance", command="balance")


class SyringeCell(Cell):
    """cell1 = syringe pump + XZ gantry, behind :class:`cell.Cell`."""

    def __init__(
        self,
        pump: SyringePumpController,
        za: MKSMotor,
        zb: MKSMotor,
        x: MKSMotor,
        config: Config,
    ) -> None:
        self._pump = pump
        self._x = x
        self._z_motors = [za, zb]
        self._cfg = config
        self._plunger_uL = 0.0
        self._stage_x_mm = 0.0
        self._stage_z_mm = 0.0
        self._initialized = False

    @classmethod
    def open(cls, config: Config) -> SyringeCell:
        pump_cfg = SyringePumpController.Config(
            port=config.pump_port,
            address=config.pump_address,
            baud=config.pump_baud,
            syringe_uL=config.syringe_uL,
            step_mode=SyringePumpController.StepMode.NORMAL,
            reply_timeout_s=2.0,
        )
        pump = SyringePumpController.open(pump_cfg)
        # Opens all three USB2CAN adapters by serial (X explicit, two Z auto).
        za, zb, x = MKSMotor.open_xz(
            config.motor_serial_x, z_coord_invert=config.z_coord_invert
        )
        return cls(pump, za, zb, x, config)

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
            # No balance on a dispense cell; ok=True so the cell isn't faulted.
            "balance": {"present": False, "ok": True},
            "stage": {  # the XZ gantry (3 MKS motors)
                "serial_x": self._cfg.motor_serial_x,
                "ok": True,
            },
            "ok_to_initialize": report.ok_to_initialize,
            "versions": {"pump": report.software_version},
        }

    def status(self) -> dict:
        return {
            "weight_g": 0.0,  # no balance on this cell
            "valve": self._pump.query_valve_position(),
            "plunger_uL": self._plunger_uL,
            "stage_x_mm": self._stage_x_mm,
            "stage_z_mm": self._stage_z_mm,
            "busy": False,
            "error": None,
        }

    # ── Balance (none on a dispense cell) ───────────────────────────────
    def tare(self) -> float:
        raise _no_balance()

    def read_weight(self) -> tuple[float, bool]:
        raise _no_balance()

    def set_ambient(self, level: str) -> str:
        raise _no_balance()

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

    # ── Stage = XZ gantry ───────────────────────────────────────────────
    # All gantry motion goes through the driver's high-level API
    # (move_sync / home_xz / stop_group_hard) and NEVER MKSMotor._send
    # directly. This is critical: those entry points run the driver's
    # _is_at_limit() check and pre-send a sacrificial command to absorb the
    # MKS-firmware quirk that drops the first motion command issued while a
    # limit switch is closed. Bypassing them would reproduce that bug.
    def _move_z(self, target: float, sp: int, ac: int) -> None:
        MKSMotor.move_sync(self._z_motors, [(target, sp, ac)])
        self._stage_z_mm = target

    def _move_x(self, target: float, sp: int, ac: int) -> None:
        MKSMotor.move_sync([self._x], [(target, sp, ac)])
        self._stage_x_mm = target

    def home_stage(self) -> tuple[float, float]:
        MKSMotor.home_xz(
            self._z_motors, self._x, self._cfg.home_dir_z, self._cfg.home_dir_x
        )
        self._stage_x_mm = 0.0
        self._stage_z_mm = 0.0
        return (0.0, 0.0)

    def move_stage(
        self, x_mm: float, z_mm: float, *, speed_pct: int, accel_pct: int
    ) -> tuple[float, float]:
        # up → X → down (never diagonal). If X is unchanged, drop Z straight.
        if x_mm == self._stage_x_mm:
            self._move_z(z_mm, speed_pct, accel_pct)
        else:
            self._move_z(0.0, speed_pct, accel_pct)  # up
            self._move_x(x_mm, speed_pct, accel_pct)  # X
            self._move_z(z_mm, speed_pct, accel_pct)  # down
        return (self._stage_x_mm, self._stage_z_mm)

    # ── Safety / lifecycle ──────────────────────────────────────────────
    def stop(self) -> None:
        # Hard-stop the whole gantry group; halt the pump if it exposes one.
        try:
            MKSMotor.stop_group_hard(self._z_motors + [self._x])
        except Exception as e:  # noqa: BLE001 — surface as a device fault
            raise DeviceFaultError(f"gantry stop failed: {e}", command="stop")
        halt = getattr(self._pump, "halt", None) or getattr(self._pump, "stop", None)
        if callable(halt):
            halt()

    def close(self) -> None:
        for dev in (self._x, *self._z_motors, self._pump):
            fn = getattr(dev, "close", None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001 — best-effort shutdown
                    print(
                        f"warning: {type(dev).__name__}.close failed", file=sys.stderr
                    )
