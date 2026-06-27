"""Cell facade + error hierarchy for the SyringeLiquidHandler /v1 server.

The L1 server is a thin HTTP bridge over a **Cell** — the composition of the
pump (``sy01b``), the balance (``entris_ii``), and the XZ stage. Two
implementations satisfy the :class:`Cell` protocol:

* :class:`SyringeCell` — real drivers, opened at the bench. Pump/balance
  calls mirror the proven sequence in ``cv_mass_measurement.py``.
* ``FakeCell`` (tests/server/conftest.py) — in-memory, for tests and for
  serving the web UI without hardware.

Every device fault surfaces as a :class:`CellError` subclass so the server
maps it to a stable HTTP status + JSON envelope (see ``server/errors.py``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class CellError(Exception):
    """Base for all cell faults.

    Carries the optional originating ``command`` and a device ``code`` so the
    server can serialize a stable error envelope without leaking tracebacks.
    """

    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.code = code


class InvalidArgError(CellError):
    """A request argument is out of range or malformed (HTTP 400)."""


class WrongStateError(CellError):
    """Operation not allowed in the current state, e.g. not initialized,
    plunger overflow (HTTP 409)."""


class DeviceFaultError(CellError):
    """A device reported a hardware fault — overload, init failure (HTTP 500)."""


class TransportError(CellError):
    """The serial/USB transport is closed or unreachable (HTTP 503)."""


class CellTimeoutError(CellError):
    """A device did not respond within its timeout (HTTP 504)."""


# Ambient-filter levels accepted by the balance (entris_ii.set_ambient).
AMBIENT_LEVELS = ("very_stable", "stable", "unstable", "very_unstable")


@runtime_checkable
class Cell(Protocol):
    """Interface the /v1 routes call. Implemented by SyringeCell + FakeCell.

    All methods are synchronous and blocking; the server runs them in a
    worker thread under a single ``asyncio.Lock`` (one command in flight).
    """

    def diagnose(self) -> dict: ...
    def status(self) -> dict: ...
    # Balance
    def tare(self) -> float: ...
    def read_weight(self) -> tuple[float, bool]: ...
    def set_ambient(self, level: str) -> str: ...
    # Pump
    def initialize(self, *, force: int = 2, ccw: bool = False) -> dict: ...
    def move_valve(self, port: int) -> str: ...
    def aspirate(self, target_uL: float) -> float: ...
    def dispense(self, target_uL: float = 0.0) -> float: ...
    def cycle(
        self,
        *,
        cycles: int,
        volume_uL: float,
        source_port: int,
        dispense_port: int,
    ) -> dict: ...
    # Stage
    def home_stage(self) -> tuple[float, float]: ...
    def move_stage(
        self, x_mm: float, z_mm: float, *, speed_pct: int, accel_pct: int
    ) -> tuple[float, float]: ...
    # Safety / lifecycle
    def stop(self) -> None: ...
    def close(self) -> None: ...
