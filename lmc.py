"""Codename ``lmc`` — the MINAS A6 linear-rail driver, re-exported.

The driver (``LinearMotorController``, from the sibling repo of the same
name) is a flat module, not a pip-installable package, so — like
``xz_stage`` does for the MKS standalone — it is added to ``sys.path`` from
the sibling repo at ``../LinearMotorController`` rather than installed into
``sdl``. Import it through this module so the rest of the cell refers to it
by the project codename:

    from lmc import LinearMotorController
"""

from __future__ import annotations

import sys
from pathlib import Path

_DRIVER_DIR = Path(__file__).resolve().parents[1] / "LinearMotorController"
if _DRIVER_DIR.is_dir() and str(_DRIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_DRIVER_DIR))

from LinearMotorController import LinearMotorController  # noqa: E402

__all__ = ["LinearMotorController"]
