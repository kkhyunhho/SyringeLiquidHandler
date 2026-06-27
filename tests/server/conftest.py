"""Pytest fixtures: FakeCell + FastAPI TestClient.

FakeCell is an in-memory stand-in satisfying the :class:`cell.Cell` protocol.
It raises the same :class:`cell.CellError` subclasses the real cell would, so
``server.errors`` maps them to HTTP status codes identically. Lives only
under tests/ — the real wiring is ``real_cell.SyringeCell``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cell import AMBIENT_LEVELS, InvalidArgError, WrongStateError
from server.app import create_app

SYRINGE_UL = 125.0


class FakeCell:
    """In-memory stand-in for the composed SyringeLiquidHandler cell."""

    def __init__(self) -> None:
        self._weight_g = 0.0
        self._valve = "?"
        self._plunger_uL = 0.0
        self._x_mm = 0.0
        self._z_mm = 0.0
        self._initialized = False
        self._ambient = "very_unstable"
        self.calls: list[str] = []

    def _require_init(self) -> None:
        if not self._initialized:
            raise WrongStateError("pump not initialized", command="initialize")

    # ── Discovery ───────────────────────────────────────────────────────
    def diagnose(self) -> dict:
        self.calls.append("diagnose")
        return {
            "pump": {"software_version": "FAKE-8.33", "valve": self._valve, "ok": True},
            "balance": {
                "model": "FAKE-BCE224I",
                "serial_number": "SN-0001",
                "ok": True,
            },
            "stage": {"enabled": True, "ok": True},
            "ok_to_initialize": True,
            "versions": {"pump": "FAKE-8.33", "balance": "FAKE-BCE224I"},
        }

    def status(self) -> dict:
        return {
            "weight_g": self._weight_g,
            "valve": self._valve,
            "plunger_uL": self._plunger_uL,
            "stage_x_mm": self._x_mm,
            "stage_z_mm": self._z_mm,
            "busy": False,
            "error": None,
        }

    # ── Balance ─────────────────────────────────────────────────────────
    def tare(self) -> float:
        self.calls.append("tare")
        self._weight_g = 0.0
        return 0.0

    def read_weight(self) -> tuple[float, bool]:
        return (self._weight_g, True)

    def set_ambient(self, level: str) -> str:
        if level not in AMBIENT_LEVELS:
            raise InvalidArgError(f"bad level {level!r}")
        self._ambient = level
        return level

    # ── Pump ────────────────────────────────────────────────────────────
    def initialize(self, *, force: int = 2, ccw: bool = False) -> dict:
        self.calls.append("initialize")
        self._initialized = True
        self._valve = "1"
        self._plunger_uL = 0.0
        return {"valve": self._valve, "plunger_uL": 0.0}

    def move_valve(self, port: int) -> str:
        self._require_init()
        self._valve = str(port)
        return self._valve

    def aspirate(self, target_uL: float) -> float:
        self._require_init()
        if not 0 <= target_uL <= SYRINGE_UL:
            raise InvalidArgError(f"target_uL 0..{SYRINGE_UL}, got {target_uL}")
        self._plunger_uL = float(target_uL)
        return self._plunger_uL

    def dispense(self, target_uL: float = 0.0) -> float:
        self._require_init()
        if not 0 <= target_uL <= SYRINGE_UL:
            raise InvalidArgError(f"target_uL 0..{SYRINGE_UL}, got {target_uL}")
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
        self._valve = str(dispense_port)
        self._plunger_uL = 0.0
        return {"cycles_done": cycles, "final_valve": self._valve}

    # ── Stage ───────────────────────────────────────────────────────────
    def home_stage(self) -> tuple[float, float]:
        self._x_mm = self._z_mm = 0.0
        return (0.0, 0.0)

    def move_stage(
        self, x_mm: float, z_mm: float, *, speed_pct: int, accel_pct: int
    ) -> tuple[float, float]:
        self._x_mm, self._z_mm = float(x_mm), float(z_mm)
        return (self._x_mm, self._z_mm)

    # ── Safety / lifecycle ──────────────────────────────────────────────
    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")


@pytest.fixture
def fake_cell() -> FakeCell:
    return FakeCell()


@pytest.fixture
def client(fake_cell: FakeCell) -> Iterator[TestClient]:
    app = create_app(cell_factory=lambda: fake_cell)
    with TestClient(app) as c:
        yield c
