"""FastAPI HTTP /v1 bridge over the SyringeLiquidHandler cell.

A thin adapter: every endpoint delegates to one method on a single
:class:`cell.Cell` held in ``app.state.cell`` (pump + balance + stage,
composed). Device-level safety and quirks live in the drivers, unchanged.
"""

from server.app import create_app

__all__ = ["create_app"]
