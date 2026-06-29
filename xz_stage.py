"""XZ gantry startup + positioning for the SyringeLiquidHandler bench.

Brings the XZ frame's three MKS SERVO57D motors (one X, paired Z_A/Z_B) up
to the measurement position before a gravimetric run: releases the kernel
``ftdi_sio`` driver so the D2XX library can claim the FTDI USB2CAN
adapters, resolves each adapter by its FTDI serial, then sets up, homes,
and moves the gantry to (``X_TARGET_MM``, ``Z_TARGET_MM``).

Uses the MKSServo57DCANController driver (`mks_motor.MKSMotor`), imported
from the sibling repo via ``sys.path`` like the balance/pump drivers.

Run by `cv_mass_measurement.py` before measuring, or standalone to bring
the frame up:  ``python3 xz_stage.py``

SAFETY: this moves a physical gantry. Clear the frame and confirm the
target and path are collision-free before running. The paired Z motors are
mechanically coupled and moved together with ``move_sync``; the underlying
driver has no desync interlock, so a mid-move comms fault on one Z can rack
the gantry — keep an e-stop within reach on the first runs.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import threading
from pathlib import Path

# This stage uses the MKSServo57DCANController *standalone* MKS driver
# (ftd2xx-based, single-axis harness), which is deliberately NOT installed
# into the shared `sdl` env — its import name `mks_motor` would collide
# with the full ESP32 driver — so add its src/ to sys.path here. The pump
# and balance drivers, by contrast, import directly from `sdl`.
# TODO: migrate to the full ESP32 mks_motor driver once the two converge.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
_MKS_SRC = _WORKSPACE_ROOT / "MKSServo57DCANController" / "src"
sys.path.insert(0, str(_MKS_SRC))

import ftd2xx  # noqa: E402
from mks_motor import MKSMotor  # noqa: E402

# ==========================================================================
# Configuration — edit these to define the frame bring-up.
# ==========================================================================

# FTDI serial of the X-axis USB2CAN adapter. The other two adapters are
# treated as the paired Z motors (Z_A / Z_B), which move in sync. List the
# attached serials with: python -c "import ftd2xx; print(ftd2xx.listDevices())"
SERIAL_X = "NTAM63XD"

# Measurement position (absolute mm, 0..MKSMotor._max_travel_mm = 450).
X_TARGET_MM = 261.5
Z_TARGET_MM = 234.0

# Motion parameters (match MKSServo57DCANController/running_test_xz.py).
HOMING_SPEED_RPM = 180
MOVE_SPEED_PCT = 25
MOVE_ACCEL_PCT = 10

# Path to the target after homing (both axes start at origin 0):
#   "z_first" — move Z to target, then X (lift/position, then traverse).
#   "x_first" — move X to target, then Z.
# Pick whichever keeps the gantry clear of the balance/vials on this bench.
MOVE_ORDER = "z_first"

log = logging.getLogger("xz_stage")


def release_ftdi_sio() -> None:
    """Detach the kernel ``ftdi_sio`` driver from every FTDI interface.

    The host kernel auto-binds ``ftdi_sio`` to FTDI adapters on every USB
    enumeration, which blocks the libusb-based D2XX library from claiming
    them. Idempotent; root-only (writes to ``/sys``).
    """
    driver_dir = "/sys/bus/usb/drivers/ftdi_sio"
    if not os.path.isdir(driver_dir):
        return
    for name in os.listdir(driver_dir):
        # Real bindings look like "3-7.2:1.0"; control files have no colon.
        if ":" not in name:
            continue
        try:
            with open(f"{driver_dir}/unbind", "w") as handle:
                handle.write(name)
            log.info("unbound ftdi_sio: %s", name)
        except OSError:
            pass


def prepare_usb_nodes() -> None:
    """Rebuild ``/dev/bus/usb`` nodes for FTDI 0403 adapters from sysfs.

    The Docker container's ``/dev`` is a private tmpfs that misses host USB
    hotplug events, so the libusb device nodes D2XX needs can go stale.
    Idempotent; root-only (calls ``os.mknod``).
    """
    usb_devices = "/sys/bus/usb/devices"
    for entry in os.listdir(usb_devices):
        vid_path = f"{usb_devices}/{entry}/idVendor"
        if not os.path.exists(vid_path):
            continue
        if open(vid_path).read().strip() != "0403":
            continue
        busnum = int(open(f"{usb_devices}/{entry}/busnum").read())
        devnum = int(open(f"{usb_devices}/{entry}/devnum").read())
        minor = (busnum - 1) * 128 + (devnum - 1)
        node = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
        os.makedirs(os.path.dirname(node), exist_ok=True)
        if not os.path.exists(node):
            os.mknod(node, 0o666 | stat.S_IFCHR, os.makedev(189, minor))
            os.chmod(node, 0o666)
            log.info("created %s", node)


def _resolve_indices() -> tuple[int, list[int]]:
    """Map FTDI serials to D2XX device indices: (x_index, [z_index, ...]).

    Returns:
        The X adapter's device index and the two Z adapter indices.

    Raises:
        RuntimeError: If ``SERIAL_X`` is missing or fewer than two other
            adapters are present.
    """
    serials = [
        s.decode() if isinstance(s, bytes) else s
        for s in (ftd2xx.listDevices() or [])
    ]
    if SERIAL_X not in serials:
        raise RuntimeError(
            f"X adapter serial {SERIAL_X!r} not found among {serials}"
        )
    x_index = serials.index(SERIAL_X)
    z_indices = [i for i in range(len(serials)) if i != x_index]
    if len(z_indices) < 2:
        raise RuntimeError(
            f"expected >=2 Z adapters, found {len(z_indices)} ({serials})"
        )
    return x_index, z_indices[:2]


def home_and_position() -> None:
    """Bring the XZ frame up and move it to the measurement position.

    Releases ``ftdi_sio``, opens the three motors by serial, sets them up,
    homes Z (paired, in parallel) then X, and moves to
    (``X_TARGET_MM``, ``Z_TARGET_MM``) in ``MOVE_ORDER``. All motors are
    closed on the way out (the servos hold position without comms).

    Raises:
        ValueError: A target is outside the driver's travel limit.
        RuntimeError: Adapter serials cannot be resolved.
    """
    limit = MKSMotor._max_travel_mm
    for axis, mm in (("X", X_TARGET_MM), ("Z", Z_TARGET_MM)):
        if not 0 <= mm <= limit:
            raise ValueError(f"{axis} target {mm} mm outside 0..{limit}")

    release_ftdi_sio()
    prepare_usb_nodes()
    x_index, z_indices = _resolve_indices()
    log.info(
        "X adapter index %d (serial %s); Z indices %s",
        x_index,
        SERIAL_X,
        z_indices,
    )

    motor_x = MKSMotor.open(port=x_index)
    motor_z = [MKSMotor.open(port=i) for i in z_indices]
    all_motors = [motor_x, *motor_z]
    try:
        for motor in all_motors:
            motor.setup()

        # Home the paired Z motors in parallel (each to its own switch),
        # then X. Both Z must home before any synced Z move.
        log.info("Homing Z (paired) at %d RPM...", HOMING_SPEED_RPM)
        threads = [
            threading.Thread(target=motor.home, args=(HOMING_SPEED_RPM,))
            for motor in motor_z
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        log.info("Homing X at %d RPM...", HOMING_SPEED_RPM)
        motor_x.home(HOMING_SPEED_RPM)

        def move_z() -> None:
            log.info("Z -> %.1f mm", Z_TARGET_MM)
            MKSMotor.move_sync(
                motor_z, [(Z_TARGET_MM, MOVE_SPEED_PCT, MOVE_ACCEL_PCT)]
            )

        def move_x() -> None:
            log.info("X -> %.1f mm", X_TARGET_MM)
            motor_x.move_to(X_TARGET_MM, MOVE_SPEED_PCT, MOVE_ACCEL_PCT)

        if MOVE_ORDER == "z_first":
            move_z()
            move_x()
        else:
            move_x()
            move_z()
        log.info(
            "XZ frame positioned: X=%.1f mm, Z=%.1f mm",
            X_TARGET_MM,
            Z_TARGET_MM,
        )
    finally:
        for motor in all_motors:
            try:
                motor.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    home_and_position()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
