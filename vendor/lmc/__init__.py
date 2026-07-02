"""Codename ``lmc`` — the MINAS A6 linear-rail driver.

The upstream driver (coport-uni/LinearMotorController) is a flat module with
no pyproject, so it can't be pip-pinned like the other drivers. It is instead
**vendored** beside its raw drivers so this cell runs standalone. Import it
through this codename module (parallel to ``vendor.sy01b`` etc.):

    from vendor.lmc import LinearMotorControllerModbus   # cell4 uses this
    from vendor.lmc import LinearMotorController          # legacy std-protocol

The amp speaks **one** protocol at a time, chosen at boot by ``Pr5.37``:

* ``LinearMotorControllerModbus`` (``Pr5.37=2``) — Modbus-RTU + Block
  Operation. Native amp-internal position mode: ``move_to_mm`` and ``home``
  run the amp's own PID/homing, so there is **no software soft-loop and no
  overshoot**. This is what :class:`BalanceLinearCell` uses.
* ``LinearMotorController`` (``Pr5.37=0``) — the older standard-protocol
  speed-mode driver with a software iterative closed loop (kept for reference
  / fallback; its per-move speed/accel is not useful — the ~5× fixed 2 s
  settle dominates and a slow decel overshoots badly).

The raw upstream drivers only accept a device path (``/dev/ttyUSBn``), which
renumbers across reboots/re-plugs. On this bench the rail is wired through a
Moxa UPort 1150 (``110A:1150``), so these thin shims add VID:PID resolution —
the one driver whose upstream lacks it (sy01b/entris_ii resolve internally).
Pass a device path or a ``"VID:PID"`` string; it is resolved at open time.
"""

from __future__ import annotations

import re

from serial.tools import list_ports

from .linear_motor_controller import (
    LinearMotorController as _LinearMotorController,
)
from .linear_motor_controller_modbus import (
    LinearMotorControllerModbus as _LinearMotorControllerModbus,
)

__all__ = [
    "LinearMotorController",
    "LinearMotorControllerModbus",
    "resolve_port",
]

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
    """Standard-protocol MINAS A6 driver that accepts a ``"VID:PID"`` port.

    Legacy speed-mode driver (``Pr5.37=0``); kept for reference/fallback.
    :class:`BalanceLinearCell` uses :class:`LinearMotorControllerModbus`.
    """

    def __init__(self, port: str) -> None:
        super().__init__(resolve_port(port))


class LinearMotorControllerModbus(_LinearMotorControllerModbus):
    """Modbus-RTU MINAS A6 driver that also accepts a ``"VID:PID"`` port.

    Requires the amp booted in Modbus-RTU + Block-Op mode (``Pr5.37=2``,
    ``Pr6.28=1``, homing params ``Pr60.52–54``; EEPROM-saved + power-cycled).
    See ``vendor/VENDORED.md`` and ``CLAUDE.md`` for the one-time amp setup.
    """

    def __init__(
        self,
        port: str,
        slave_id: int = 1,
        baudrate: int = 9600,
        timeout: float = 1.0,
    ) -> None:
        super().__init__(
            resolve_port(port),
            slave_id=slave_id,
            baudrate=baudrate,
            timeout=timeout,
        )
