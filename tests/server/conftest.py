"""Pytest fixtures: FakeCell + FastAPI TestClient.

The FakeCell lives in ``fake_cell.py`` (shared with ``python -m server
--fake``); here we only wire it into a TestClient.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from fake_cell import FakeCell
from server.app import create_app


@pytest.fixture
def fake_cell() -> FakeCell:
    return FakeCell()


@pytest.fixture
def client(fake_cell: FakeCell) -> Iterator[TestClient]:
    app = create_app(cell_factory=lambda: fake_cell)
    with TestClient(app) as c:
        yield c
