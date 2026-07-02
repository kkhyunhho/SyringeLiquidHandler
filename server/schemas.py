"""Pydantic request/response models for the SyringeLiquidHandler /v1 API.

Units are in field names (``_g`` grams, ``_uL`` microliters, ``_mm``
millimeters, ``_pct`` percent) to match the SDLClaude UI unit standard.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── Discovery ──────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    cell_up: bool
    pump_ok: bool | None
    balance_ok: bool | None
    stage_ok: bool | None
    driver_versions: dict[str, str]


class DiagnoseResponse(BaseModel):
    pump: dict = Field(description="Pump diagnostics (version, valve, …).")
    balance: dict = Field(description="Balance model + serial number.")
    stage: dict = Field(description="Stage status (per-axis).")
    ok_to_initialize: bool


class StatusResponse(BaseModel):
    weight_g: float
    valve: str = Field(description="Current valve position label, e.g. '1'.")
    plunger_uL: float
    stage_x_mm: float
    stage_z_mm: float
    busy: bool
    error: str | None


class ErrorResponse(BaseModel):
    error: str
    code: int | None
    command: str | None
    message: str


# ── Balance ────────────────────────────────────────────────────────────────


class WeightResponse(BaseModel):
    weight_g: float


class WeightReadResponse(BaseModel):
    weight_g: float
    stable: bool


class AmbientRequest(BaseModel):
    level: str = Field(description="very_stable | stable | unstable | very_unstable")


class AmbientResponse(BaseModel):
    level: str


# ── Pump ───────────────────────────────────────────────────────────────────


class InitializeRequest(BaseModel):
    force: int = Field(default=2, description="0/1/2 or 10..40 init force code.")
    ccw: bool = False


class InitializeResponse(BaseModel):
    valve: str
    plunger_uL: float


class ValveRequest(BaseModel):
    port: int = Field(ge=1, le=4, description="Valve port (1 or 3 in use).")


class ValveResponse(BaseModel):
    valve: str


class VolumeRequest(BaseModel):
    target_uL: float = Field(
        ge=0, description="Absolute contained-volume target in µL."
    )


class PlungerResponse(BaseModel):
    plunger_uL: float


class CycleRequest(BaseModel):
    cycles: int = Field(ge=1, le=50)
    volume_uL: float = Field(gt=0)
    source_port: int = Field(ge=1, le=4)
    dispense_port: int = Field(ge=1, le=4)


class CycleResponse(BaseModel):
    cycles_done: int
    final_valve: str


# ── Gantry (XZ) ──────────────────────────────────────────────────────────────


class GantryMoveRequest(BaseModel):
    x_mm: float = Field(ge=0)
    z_mm: float = Field(ge=0)
    speed_pct: int = Field(default=20, ge=1, le=100)
    accel_pct: int = Field(default=10, ge=1, le=100)


class GantryResponse(BaseModel):
    x_mm: float
    z_mm: float


# ── Linear (Y) ───────────────────────────────────────────────────────────────


class LinearMoveRequest(BaseModel):
    y_mm: float = Field(ge=0)


class LinearResponse(BaseModel):
    y_mm: float


# ── Safety ─────────────────────────────────────────────────────────────────


class StopResponse(BaseModel):
    stopped: bool
