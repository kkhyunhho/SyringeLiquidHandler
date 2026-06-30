"""Codename ``lmc`` — the MINAS A6 linear-rail driver.

The upstream driver (coport-uni/LinearMotorController) is a flat module with
no pyproject, so it can't be pip-pinned like the other drivers. It is instead
**vendored** under ``vendor/`` (one file) so this cell runs standalone. Import
it through this module so the rest of the cell refers to it by codename:

    from lmc import LinearMotorController
"""

from __future__ import annotations

from vendor.linear_motor_controller import LinearMotorController

__all__ = ["LinearMotorController"]
