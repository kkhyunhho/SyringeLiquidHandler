"""MKS SERVO57D CAN motor driver (PC side).

Public surface re-exported so callers import a single name:

    from mks_motor import MKSMotor, prepare_usb_nodes, release_ftdi_sio
"""

from .mks_motor import (
    MKSMotor,
    prepare_usb_nodes,
    release_ftdi_sio,
    set_group_fault_hook,
)

__all__ = [
    "MKSMotor",
    "prepare_usb_nodes",
    "release_ftdi_sio",
    "set_group_fault_hook",
]
