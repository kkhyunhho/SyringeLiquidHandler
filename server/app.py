"""FastAPI application factory for the SyringeLiquidHandler cell.

``create_app(cell_factory=None)`` mirrors the sy01b-server pattern: inject a
real :class:`SyringeCell` in production and a ``FakeCell`` in tests/dev. The
factory owns the lifespan — it builds the cell once on startup and closes it
on shutdown.

``app.state`` fields:
- ``cell``: the composed :class:`Cell` (pump + balance + stage).
- ``lock``: ``asyncio.Lock`` serializing every device interaction.
- ``last_diagnose``: cached diagnose dict (None until first GET /v1/diagnose).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from server.errors import register_exception_handlers
from server.routes import router

CellFactory = Callable[[], Any]


def create_app(cell_factory: CellFactory | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if cell_factory is not None:
            app.state.cell = cell_factory()
        app.state.lock = asyncio.Lock()
        app.state.last_diagnose = None
        try:
            yield
        finally:
            cell = getattr(app.state, "cell", None)
            close = getattr(cell, "close", None)
            if callable(close):
                close()

    app = FastAPI(
        title="slh-server",
        version="0.1.0",
        description=(
            "HTTP /v1 bridge over the SyringeLiquidHandler cell — pump "
            "(sy01b) + balance (entris_ii) + XZ stage — composed behind one "
            "Cell facade. Endpoints mirror the web UI's device tabs."
        ),
        openapi_tags=[
            {
                "name": "Discovery",
                "description": (
                    "Read-only probes (health, diagnose, status). Safe to "
                    "call repeatedly — never moves a device."
                ),
            },
            {
                "name": "Balance",
                "description": "Sartorius Entris-II: tare, settled read, ambient filter.",
            },
            {
                "name": "Pump",
                "description": (
                    "Runze SY-01B: initialize, valve, aspirate/dispense, and "
                    "the unified repeated cycle (prime / dispense)."
                ),
            },
            {
                "name": "Stage",
                "description": "XZ gantry (MKS SERVO57D): home and move.",
            },
            {
                "name": "Safety",
                "description": "Abort all motion now.",
            },
        ],
        lifespan=lifespan,
    )
    app.include_router(router)
    register_exception_handlers(app)
    return app
