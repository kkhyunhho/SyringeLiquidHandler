"""Codename ``lmc`` — the MINAS A6 linear-rail driver.

The upstream driver (coport-uni/LinearMotorController) is a flat module with
no pyproject, so it can't be pip-pinned like the other drivers. It is instead
**vendored** beside its raw driver so this cell runs standalone. Import it
through this codename module (parallel to ``vendor.sy01b`` etc.):

    from vendor.lmc import LinearMotorController

The driver talks the **MINAS standard serial protocol over RS485**
(ENQ/EOT/ACK/NAK, 9600 8N1; amp ``Pr5.37=0``) — not Modbus. Absolute
positioning (``move_to_mm``) runs a software closed loop whose per-iteration
speed command comes from a ``PIDController`` (P-tuned by default), so
speed-mode overshoot collapses into the tolerance band and the loop aborts if
the residual stops shrinking.

The raw upstream driver (``vendor/lmc/linear_motor_controller.py``) only
accepts a device path (``/dev/ttyUSBn``), which renumbers across
reboots/re-plugs. On this bench the rail is wired through a Moxa UPort 1150
(``110A:1150``), so this thin shim adds VID:PID resolution — the one driver
whose upstream lacks it (sy01b/entris_ii resolve internally). Pass a device
path or a ``"VID:PID"`` string; it is resolved at open time.
"""

from __future__ import annotations

import re

from serial.tools import list_ports

from .linear_motor_controller import (
    LinearMotorController as _LinearMotorController,
)

__all__ = ["LinearMotorController", "resolve_port"]

_VIDPID_RE = re.compile(r"^([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})$")


def resolve_port(port: str) -> str:
    """Resolve a ``"VID:PID"`` string to a ``/dev/ttyUSBn`` path.

    A plain device path is returned unchanged. A ``"VID:PID"`` string (e.g.
    ``"110A:1150"``) is matched against the connected serial adapters so the
    rail survives ttyUSBn renumbering.

    Raises:
        RuntimeError: if no connected adapter matches the VID:PID.
    """
    m = _VIDPID_RE.match(port.strip())
    if not m:
        return port  # already a device path
    vid, pid = int(m.group(1), 16), int(m.group(2), 16)
    matches = sorted(
        p.device for p in list_ports.comports() if p.vid == vid and p.pid == pid
    )
    if not matches:
        raise RuntimeError(f"no serial adapter with VID:PID {port} connected")
    return matches[0]


class LinearMotorController(_LinearMotorController):
    """MINAS A6 RS485 driver that also accepts a ``"VID:PID"`` port string."""

    def __init__(self, port: str) -> None:
        super().__init__(resolve_port(port))
