"""Cell exception → HTTP JSON response mapping.

``register_exception_handlers`` wires each :class:`CellError` subclass to a
JSONResponse with a stable envelope (``schemas.ErrorResponse``). No traceback
leaks; the cell attaches ``command`` / ``code`` and we serialize those.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cell.cell_protocol import (
    CellError,
    CellTimeoutError,
    DeviceFaultError,
    InvalidArgError,
    TransportError,
    WrongStateError,
)


def _body(exc: CellError) -> dict[str, object]:
    return {
        "error": type(exc).__name__,
        "code": exc.code,
        "command": exc.command,
        "message": str(exc),
    }


def _plain(exc: Exception) -> dict[str, object]:
    return {
        "error": type(exc).__name__,
        "code": None,
        "command": None,
        "message": str(exc),
    }


# Most specific first; the base CellError catches anything unmapped as 500.
_STATUS: list[tuple[type[CellError], int]] = [
    (InvalidArgError, 400),
    (WrongStateError, 409),
    (TransportError, 503),
    (CellTimeoutError, 504),
    (DeviceFaultError, 500),
]


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers mapping cell exceptions to JSON responses."""

    @app.exception_handler(CellError)
    async def _cell_error(_req: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, CellError)
        status = next(
            (s for t, s in _STATUS if isinstance(exc, t)),
            500,  # bare CellError / unmapped subclass = device fault
        )
        return JSONResponse(status_code=status, content=_body(exc))

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=400, content=_plain(exc))
