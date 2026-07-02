# mks_motor.py
# MKS SERVO57D CAN motor library, accessed over a per-motor USB2CAN
# (FTDI FT232R) adapter using libusb via pyftdi.
#
# Architecture (bottom to top):
#   USB2CAN binary packet (18 bytes) wraps a CAN frame (≤8 bytes)
#   that carries [cmd_code] [data...] [checksum] for the motor.
#
# Reading Guide:
#   1. Module helpers      - prepare_usb_nodes / release_ftdi_sio
#                            (Docker /dev recovery + ftdi_sio unbind)
#   2. open / open_xz      - FTDI adapter open by serial number
#   3. _send / _wait       - Core CAN communication
#   4. setup / enable_endstops / home  - Motor initialization
#   5. move_to / _jog      - High-level motion commands
#                            (both gated by _is_at_limit() to absorb
#                             the firmware's "first command after
#                             limit-stop is dropped" quirk; _jog is
#                             the internal primitive — public callers
#                             use jog_start / jog_stop)
#   6. *_sync helpers      - Multi-motor parallel control

import os
import stat
import threading
import time

from pyftdi.ftdi import Ftdi


def prepare_usb_nodes():
    """Recreate USB device nodes from sysfs.

    The Docker container's /dev is a private tmpfs that doesn't
    receive host USB hotplug events, so device nodes go stale
    after every re-enumeration. This rebuilds:
      - /dev/bus/usb/<bus>/<dev> for FTDI 0403:6001 adapters
        (the libusb path used by pyftdi)
      - /dev/ttyACM* for the ESP32-S3-BOX-3 CDC serial

    Idempotent — skips nodes that already exist.
    Root-only (calls os.mknod).
    """
    # FTDI raw USB nodes
    for d in os.listdir('/sys/bus/usb/devices'):
        vid_path = f'/sys/bus/usb/devices/{d}/idVendor'
        if not os.path.exists(vid_path):
            continue
        if open(vid_path).read().strip() != '0403':
            continue
        busnum = int(open(f'/sys/bus/usb/devices/{d}/busnum').read())
        devnum = int(open(f'/sys/bus/usb/devices/{d}/devnum').read())
        minor = (busnum - 1) * 128 + (devnum - 1)
        node = f'/dev/bus/usb/{busnum:03d}/{devnum:03d}'
        os.makedirs(os.path.dirname(node), exist_ok=True)
        if not os.path.exists(node):
            os.mknod(node, 0o666 | stat.S_IFCHR, os.makedev(189, minor))
            os.chmod(node, 0o666)
            print(f"created {node} (minor={minor})")

    # CDC-ACM serial nodes (ESP32 + any other ACM device).
    # major:minor is published by the kernel at /sys/class/tty/<name>/dev.
    for name in sorted(os.listdir('/sys/class/tty')):
        if not name.startswith('ttyACM'):
            continue
        dev_attr = f'/sys/class/tty/{name}/dev'
        if not os.path.exists(dev_attr):
            continue
        major, minor = (int(x) for x in open(dev_attr).read().strip().split(':'))
        node = f'/dev/{name}'
        if not os.path.exists(node):
            os.mknod(node, 0o666 | stat.S_IFCHR, os.makedev(major, minor))
            os.chmod(node, 0o666)
            print(f"created {node} ({major}:{minor})")


def release_ftdi_sio():
    """Detach the in-kernel ftdi_sio driver from every FTDI interface
    so libusb-based access (pyftdi) can enumerate cleanly.

    Required because the host kernel auto-binds ftdi_sio to FTDI
    devices on every USB enumeration. Idempotent and root-only
    (writes to /sys).
    """
    driver_dir = '/sys/bus/usb/drivers/ftdi_sio'
    if not os.path.isdir(driver_dir):
        return
    for name in os.listdir(driver_dir):
        # Real bindings look like '3-3.2:1.0'; driver control files
        # ('bind', 'unbind', 'module', 'uevent') have no colon.
        if ':' not in name:
            continue
        try:
            with open(f'{driver_dir}/unbind', 'w') as f:
                f.write(name)
            print(f"unbound ftdi_sio: {name}")
        except OSError:
            pass


# Application-supplied callback fired when a paired-group operation cannot
# be applied to every motor (e.g. a CAN link dropped mid-jog/move). The
# group is emergency-stopped BEFORE this fires; the hook lets the
# application decide what to do next — typically stop every axis and
# abort. The motor library never terminates the process itself. None =
# no-op. Set via set_group_fault_hook().
_GROUP_FAULT_HOOK = None


def set_group_fault_hook(fn):
    """Register a callback invoked on an unrecoverable paired-group fault.

    Args:
        fn: Callable taking a single reason string, or None to clear it.
            Fired AFTER the faulting group has been emergency-stopped,
            from whichever thread detected the fault.
    """
    global _GROUP_FAULT_HOOK
    _GROUP_FAULT_HOOK = fn


def _fire_group_fault(reason):
    """Invoke the registered group-fault hook if one is set."""
    hook = _GROUP_FAULT_HOOK
    if hook is not None:
        hook(reason)


class MKSMotor:
    """Controls MKS SERVO57D via USB2CAN-FIFO adapter."""

    # --- Constants ---

    # Mechanical / motor limits
    _mm_per_turn = 10
    _encoder_per_turn = 16384
    _max_speed_rpm = 3000
    _max_accel = 255
    _max_travel_mm = 400
    _max_wait_sec = 250

    # FTDI device setup (USB2CAN-FIFO adapter)
    _ftdi_vid = 0x0403
    _ftdi_pid = 0x6001
    _ftdi_bitmode_mask = 0xFF
    _ftdi_bitmode_value = 0x40  # SYNC_FIFO mode byte (matches original ftd2xx value)
    _ftdi_latency_ms = 2        # tight latency for fast CAN response polling

    # CAN response read retry policy
    _response_retry_count = 10
    _response_retry_delay_s = 0.1

    # Tuned timing / counts for the bring-up workflow.
    _default_home_speed_rpm = 180   # default Hm_Speed if caller omits it
    _setup_retry_count = 3          # firmware sometimes drops the very
                                    # first setting command after a fresh
                                    # CAN connection
    _jog_dir_bit = 0x80             # F6 byte 2 bit 7: 1 = CW, 0 = CCW

    # MKS firmware quirk: the first motion command (F4/F5/F6) sent
    # while a limit switch is closed is silently dropped. Every
    # motion entry-point (jog, move_to) checks _is_at_limit() first
    # and pre-sends a sacrificial copy of the command to absorb the
    # dropped slot. No physical dummy motion required.

    # MKS command opcodes used by this file.
    CMD_READ_ENCODER    = 0x31   # read cumulative multi-turn encoder (abs pos)
    CMD_READ_IO         = 0x34   # read IN_1/IN_2/OUT_1/OUT_2 state
    CMD_SET_MODE        = 0x82   # 0x05 selects SR_vFOC
    CMD_SLAVE_RESP      = 0x8C   # enable active response from the motor
    CMD_SET_HOME        = 0x90   # set homing parameters
    CMD_START_HOME      = 0x91   # execute homing (uses last 0x90 settings)
    CMD_MOVE_TO         = 0xF5   # absolute-coord motion
    CMD_JOG             = 0xF6   # speed-mode jog
    CMD_ESTOP           = 0xF7   # emergency stop (hard, no decel ramp)

    # Status bytes returned by the motor. The labels live in the
    # _motion_status / _setting_status dicts below for pretty-print;
    # these constants are for code comparisons.
    STATUS_FAILED        = 0x00   # setting OR motion failure
    STATUS_SUCCESS       = 0x01   # setting OK (also = "Running" for motion)
    STATUS_RUNNING       = 0x01
    STATUS_COMPLETE      = 0x02
    STATUS_LIMIT_STOPPED = 0x03

    # Commands that report a motion-status code (Running / Complete /
    # Stopped by Limit) rather than a setting-status code (Success /
    # Fail). Narrowed to what we actually send.
    _motion_cmds = {CMD_START_HOME, CMD_MOVE_TO, CMD_JOG}

    _motion_status = {
        STATUS_FAILED:        "Failed",
        STATUS_RUNNING:       "Running",
        STATUS_COMPLETE:      "Complete",
        STATUS_LIMIT_STOPPED: "Stopped by Limit",
        0x05:                 "Sync Data Received",
    }

    _setting_status = {
        STATUS_FAILED:  "Failed",
        STATUS_SUCCESS: "Success",
    }

    # --- Construction ---

    def __init__(self, dev, can_id=0x01, coord_invert=False):
        self.dev = dev
        self.can_id = can_id
        # When True, F5/F4 coord values are negated before being sent.
        # Use on axes where the motor's "positive encoder direction"
        # points INTO the closed home limit (e.g. after physically
        # swapping the IN_1/IN_2 limit wires). F6 jog is NOT affected;
        # the user's CW/CCW handler mapping stays as-is.
        self.coord_invert = coord_invert

    @classmethod
    def open(cls, port=0, can_id=0x01, serial=None, coord_invert=False):
        """Open FTDI device and return a ready MKSMotor.

        Args:
            port: FTDI device enumeration index. Ignored when
                serial is provided. Defaults to 0.
            can_id: CAN bus ID for this motor.
            serial: FTDI chip serial number (e.g. "NTAM63XD").
                When set, the adapter is picked by serial rather
                than enumeration index — robust against USB
                re-enumeration order changes.
            coord_invert: Forwarded to MKSMotor.__init__. Flips the
                sign of F5/F4 coord values so move_to(+mm) moves the
                motor away from home even when its encoder positive
                direction points into the closed home limit.

        Returns:
            Configured MKSMotor instance.
        """
        dev = Ftdi()
        if serial is not None:
            dev.open(cls._ftdi_vid, cls._ftdi_pid, serial=serial)
        else:
            dev.open(cls._ftdi_vid, cls._ftdi_pid, index=port)
        dev.set_bitmode(cls._ftdi_bitmode_mask, Ftdi.BitMode(cls._ftdi_bitmode_value))
        dev.set_latency_timer(cls._ftdi_latency_ms)
        dev.purge_buffers()
        # FTDI bitmode + latency take a moment to settle; without this
        # delay the first _send sometimes returns no response.
        time.sleep(0.3)
        return cls(dev, can_id, coord_invert=coord_invert)

    @classmethod
    def open_xz(cls, serial_x, z_coord_invert=True):
        """Open all three USB2CAN adapters by FTDI serial.

        Picks the X adapter explicitly by serial; whichever two FTDI
        adapters remain are assigned to Z_A and Z_B. Z order doesn't
        matter because the paired Z motors always move together.

        Args:
            serial_x: FTDI chip serial of the X-axis adapter
                (e.g. "NTAM63XD").
            z_coord_invert: Apply coord_invert to the Z motors only.
                Default True for this project because the Z limit
                wires were physically swapped, which flips the
                meaning of "positive coord" for those motors. Set
                False if your Z wiring matches X's.

        Returns:
            Tuple (za, zb, x) of MKSMotor instances.

        Raises:
            RuntimeError: If serial_x is not present or fewer than
                two Z adapters remain.
        """
        all_serials = [url.sn for url, _ in Ftdi.list_devices()]
        if serial_x not in all_serials:
            raise RuntimeError(
                f"X adapter (serial={serial_x}) not connected. "
                f"Found adapters: {all_serials}"
            )
        z_serials = [s for s in all_serials if s != serial_x]
        if len(z_serials) < 2:
            raise RuntimeError(
                f"Need 2 Z adapters, only found {len(z_serials)}: {z_serials}"
            )
        print(f"  X  = {serial_x}")
        print(f"  Z_A = {z_serials[0]}")
        print(f"  Z_B = {z_serials[1]}")
        za = cls.open(serial=z_serials[0], coord_invert=z_coord_invert)
        zb = cls.open(serial=z_serials[1], coord_invert=z_coord_invert)
        x  = cls.open(serial=serial_x)
        return za, zb, x

    def close(self):
        """Close the underlying FTDI device."""
        if self.dev:
            self.dev.close()
            print("Device closed.")

    # --- Internal helpers ---

    @staticmethod
    def _clamp(value, low, high):
        """Enforce that value is within [low, high].

        Args:
            value: Number to check.
            low: Lower bound (inclusive).
            high: Upper bound (inclusive).

        Returns:
            The original value if within range.

        Raises:
            ValueError: If value is outside [low, high].
        """
        if value < low or value > high:
            raise ValueError(f"Value {value} out of range [{low}, {high}]")
        return value

    @staticmethod
    def _int16_bytes(value):
        """Split uint16 into [high, low] bytes.

        Args:
            value: Unsigned 16-bit integer.

        Returns:
            List of two bytes [high, low].
        """
        return [(value >> 8) & 0xFF, value & 0xFF]

    @staticmethod
    def _int24_bytes(value):
        """Split int24 into [high, mid, low] bytes.

        Args:
            value: 24-bit integer.

        Returns:
            List of three bytes [high, mid, low].
        """
        value_24bit = value & 0xFFFFFF
        return [
            (value_24bit >> 16) & 0xFF,
            (value_24bit >> 8) & 0xFF,
            value_24bit & 0xFF,
        ]

    # --- Unit conversions ---

    def _pct_to_speed(self, pct):
        """Convert percentage to motor RPM.

        Maps 0-100% linearly onto [0, _max_speed_rpm].

        Args:
            pct: Speed percentage (0-100).

        Returns:
            Integer RPM value.

        Raises:
            ValueError: If pct is outside [0, 100].
        """
        return int(self._max_speed_rpm * self._clamp(pct, 0, 100) / 100)

    def _pct_to_accel(self, pct):
        """Convert percentage to motor acceleration.

        Maps 0-100% linearly onto [0, _max_accel].

        Args:
            pct: Acceleration percentage (0-100).

        Returns:
            Integer acceleration value.

        Raises:
            ValueError: If pct is outside [0, 100].
        """
        return int(self._max_accel * self._clamp(pct, 0, 100) / 100)

    def _mm_to_coord(self, mm):
        """Convert mm distance to encoder coordinate.

        Sign-flipped when self.coord_invert is True so that
        callers can always pass non-negative mm and have move_to /
        dummy moves go in the physically correct direction (away
        from the closed home limit).

        Args:
            mm: Distance in millimeters.

        Returns:
            Integer encoder count (negative when coord_invert).

        Raises:
            ValueError: If mm is outside
                [0, _max_travel_mm].
        """
        coord = int(
            self._clamp(mm, 0, self._max_travel_mm)
            / self._mm_per_turn
            * self._encoder_per_turn
        )
        if self.coord_invert:
            coord = -coord
        return coord

    # --- Low-level: CAN packet over USB2CAN ---

    def _transceive(self, cmd, *data, silent=False):
        """Send a CAN command and return the raw 18-byte USB2CAN response.

        Shared TX + retry-read core. _send() uses this and returns just
        the status byte; multi-byte readers (read_position_mm) use it to
        parse the full payload.

        Builds [cmd][data...][checksum] padded to 8 bytes, wraps it in an
        18-byte USB2CAN binary packet, and reads the reply.

        Args:
            cmd: MKS command code (e.g. 0xF5).
            *data: Variable-length data bytes.
            silent: Suppress TX logging if True.

        Returns:
            The 18-byte response (bytes), or None for a broadcast ID
            (no response expected).

        Raises:
            ValueError: If the payload exceeds the 8-byte CAN frame.
            ConnectionError: If no valid response after retries.
        """
        data_bytes = list(data)
        dlc = 1 + len(data_bytes) + 1
        if dlc > 8:
            raise ValueError(f"Too much data ({dlc} bytes, max 8)")

        checksum = (self.can_id + cmd + sum(data_bytes)) & 0xFF
        motor_bytes = [cmd] + data_bytes + [checksum]
        motor_bytes += [0x00] * (8 - len(motor_bytes))

        # USB2CAN binary packet (18 bytes total)
        # See USB2CAN manual section 3.2.2
        packet = bytearray(18)
        packet[0] = 0x02  # STX
        packet[1] = 0x00  # Type
        packet[2] = dlc  # DLC
        packet[3] = 0x00  # Flags
        packet[4:8] = self.can_id.to_bytes(  # CAN ID
            4, "little"
        )
        packet[8:16] = bytes(motor_bytes)  # Data
        packet[16] = sum(packet[1:16]) & 0xFF  # Checksum
        packet[17] = 0x03  # ETX

        self.dev.purge_rx_buffer()
        self.dev.write_data(bytes(packet))
        if not silent:
            data_hex = bytes(data_bytes).hex().upper() or "(no data)"
            print(f"[TX] 0x{cmd:02X} {data_hex}")

        if self.can_id == 0x00:
            if not silent:
                print("[TX] Broadcast -- no response expected")
            return None

        resp = b""
        for _ in range(self._response_retry_count):
            time.sleep(self._response_retry_delay_s)
            resp = self.dev.read_data_bytes(18)
            if len(resp) == 18:
                break
            self.dev.purge_rx_buffer()

        if len(resp) != 18:
            raise ConnectionError(
                f"No response for 0x{cmd:02X} -- check CAN wiring, power, and bitrate"
            )
        return resp

    def _send(self, cmd, *data, silent=False):
        """Send a CAN command and return the status byte.

        Thin wrapper over _transceive that extracts the single status
        byte (resp[9]) and pretty-prints it.

        Args:
            cmd: MKS command code (e.g. 0xF5).
            *data: Variable-length data bytes.
            silent: Suppress TX/RX logging if True.

        Returns:
            Status byte from the motor response, or None if broadcast.

        Raises:
            ConnectionError: If no valid response after retries.
        """
        resp = self._transceive(cmd, *data, silent=silent)
        if resp is None:
            return None
        status = resp[9]
        if not silent:
            if cmd in self._motion_cmds:
                table = self._motion_status
            else:
                table = self._setting_status
            status_label = table.get(status, f"Unknown 0x{status:02X}")
            print(f"[RX] {status_label}")
        return status

    def read_position_mm(self):
        """Read the motor's absolute position in mm (CAN 0x31).

        Reads the cumulative multi-turn encoder value — a signed 48-bit
        count where one full turn = ``_encoder_per_turn`` — and converts
        it to mm via the ball-screw pitch. Honors ``coord_invert`` so the
        sign matches the convention move_to() takes.

        Returns:
            Position in mm (float), or None if the reply was a stray
            frame (wrong command echo).

        Raises:
            ConnectionError: If the motor does not respond after retries.
        """
        resp = self._transceive(self.CMD_READ_ENCODER, silent=True)
        if resp is None:
            return None
        # Motor payload occupies resp[8:16]: [0x31][value 6B][checksum].
        # Reject a stray frame whose command echo isn't our read.
        if resp[8] != self.CMD_READ_ENCODER:
            return None
        value = int.from_bytes(resp[9:15], "big", signed=True)  # int48
        mm = value / self._encoder_per_turn * self._mm_per_turn
        return -mm if self.coord_invert else mm

    def _wait(self):
        """Wait for async motor response.

        Blocks until the motor reports completion, failure, or
        limit hit. Timeout resets each time a "Running" response
        arrives. The "first motion command after limit-stop" skip
        is no longer cleared here — the next motion call handles
        it via _is_at_limit() like _jog() / move_to() do.

        Returns:
            Status byte (0x02=complete, 0x03=limit, etc.), or None
            on timeout.
        """
        deadline = time.time() + self._max_wait_sec

        while time.time() < deadline:
            resp = self.dev.read_data_bytes(18)
            if len(resp) == 18:
                status = resp[9]
                label = self._motion_status.get(status, f"0x{status:02X}")
                print(f"[RX] {label}")

                if status == self.STATUS_RUNNING:
                    deadline = time.time() + self._max_wait_sec
                    continue
                if status == self.STATUS_LIMIT_STOPPED:
                    print("[LIMIT] Motor stopped by limit switch")
                return status

            # No frame yet — sleep a short poll interval so we don't
            # spin the CPU while the motor is still moving.
            time.sleep(0.1)

        print("[ERROR] Motor not responding -- check power, wiring, and CAN")
        return None

    # --- State queries ---

    def _read_io_status(self):
        """Read IO port status (CMD_READ_IO).

        Returns:
            Bit-packed byte with:
              bit0 = IN_1 (home / left-limit)
              bit1 = IN_2 (right-limit)
              bit2 = OUT_1
              bit3 = OUT_2
            Or None if no response.

            With our setup (homeTrig=0, "active low"), a bit value
            of 0 means the corresponding switch is closed.
        """
        return self._send(self.CMD_READ_IO, silent=True)

    def _is_at_limit(self):
        """True if either limit switch is currently closed.

        Used by _jog() and move_to() to decide whether the MKS
        firmware's "first motion command after limit-stop is dropped"
        workaround is needed.
        """
        status = self._read_io_status()
        if status is None:
            return False
        # bit0 = IN_1, bit1 = IN_2. Active low (0 = closed).
        return (status & 0x01) == 0 or (status & 0x02) == 0

    # --- Setup & Homing ---

    def _retry_setting(self, action):
        """Run a setting action, retrying the MKS first-command drop.

        MKS firmware occasionally ignores the first command after a
        fresh CAN connection. Retries up to ``_setup_retry_count`` times.

        Args:
            action: Zero-arg callable returning True on success. A CAN
                timeout (ConnectionError) counts as a failed attempt.

        Returns:
            True if any attempt succeeded.
        """
        for _ in range(self._setup_retry_count):
            try:
                if action():
                    return True
            except ConnectionError:
                pass
        return False

    def setup(self):
        """Apply default motor settings.

        Configures SR_vFOC mode and full slave response.

        Returns:
            True if all settings applied,
            False otherwise.
        """
        commands = [
            (self.CMD_SET_MODE,   [0x05]),         # 0x05 = SR_vFOC
            (self.CMD_SLAVE_RESP, [0x01, 0x01]),   # enable active response
        ]
        # Each command is retried because MKS firmware occasionally drops
        # the first command after a fresh CAN connection.
        ok = True
        for cmd, data in commands:
            if not self._retry_setting(
                    lambda c=cmd, d=data:
                    self._send(c, *d, silent=True) == self.STATUS_SUCCESS):
                ok = False

        if ok:
            print("[SETUP] OK")
        else:
            print("[SETUP] FAILED")
        return ok

    def _home_payload(self, *, home_trig=0x00, direction=0x01,
                      speed_rpm=None, end_limit=False, hm_mode=0x00):
        """Build the 6-byte payload for CMD_SET_HOME (0x90).

        Args:
            home_trig: Effective level when the home switch is closed.
                0 = active-low (closed = 0), 1 = active-high.
            direction: homeDir byte — direction the motor moves to
                search for the home switch. 0x00 / 0x01.
            speed_rpm: Hm_Speed (0-3000). Defaults to
                ``_default_home_speed_rpm`` if None.
            end_limit: True to enable the left/right limit switches
                during subsequent motion. False during the homing
                pass itself.
            hm_mode: 0 = origin switch, 1 = mechanical-limit stall,
                2 = single-lap zeroing.

        Returns:
            List of 6 bytes ready to pass as ``*data`` to ``_send``.
        """
        if speed_rpm is None:
            speed_rpm = self._default_home_speed_rpm
        return [
            home_trig,
            direction,
            *self._int16_bytes(speed_rpm),
            0x01 if end_limit else 0x00,
            hm_mode,
        ]

    def enable_endstops(self, direction=0x01, speed_rpm=None):
        """Arm the left/right limit switches without running homing.

        Sends CMD_SET_HOME with end_limit=True so subsequent F4/F5/F6
        motion stops when a limit switch is triggered. Homing
        parameters (direction, speed) are written too because the
        underlying 0x90 is a single composite command — pass the
        same direction you would hand to home() so a later home()
        doesn't need to reconfigure.

        Args:
            direction: homeDir byte (0x00 / 0x01).
            speed_rpm: Hm_Speed; falls back to
                ``_default_home_speed_rpm`` when omitted.

        Returns:
            True if the motor accepted the command.
        """
        payload = self._home_payload(
            direction=direction, speed_rpm=speed_rpm, end_limit=True
        )
        return self._send(self.CMD_SET_HOME, *payload,
                          silent=True) == self.STATUS_SUCCESS

    def home(self, speed_rpm=None, direction=0x01):
        """Run homing sequence and enable limit switches.

        Finds the origin switch, sets the zero point, then enables
        the limit switches for safe operation.

        HARDWARE NOTE: Motor direction is physically inverted due to
        wiring/mounting. Manual says 0x00=CW / 0x01=CCW, but actual
        movement is opposite.

        Args:
            speed_rpm: Homing speed in RPM; falls back to
                ``_default_home_speed_rpm`` when omitted.
            direction: homeDir byte. The motor travels in this
                direction during homing, so whichever limit switch
                sits on that side becomes the origin. Flip
                0x00 <-> 0x01 to home off the opposite limit.
        """
        if speed_rpm is None:
            speed_rpm = self._default_home_speed_rpm
        print(
            f"{'=' * 40}\n"
            f"HOMING (speed={speed_rpm} RPM, dir=0x{direction:02X})\n"
            f"{'=' * 40}"
        )

        # Homing pass: limits disabled so the motor can roll onto the
        # home switch without the firmware refusing the motion.
        self._send(self.CMD_SET_HOME, *self._home_payload(
            direction=direction, speed_rpm=speed_rpm, end_limit=False))
        self._send(self.CMD_START_HOME)
        print("Moving toward origin switch...")
        status = self._wait()

        if status == self.STATUS_COMPLETE:
            print("Homing complete. Zero point set.")
            self._send(self.CMD_SET_HOME, *self._home_payload(
                direction=direction, speed_rpm=speed_rpm, end_limit=True))
            print("Limit switches enabled.")
            # Motor sits exactly on the home switch (encoder=0). The
            # next motion call (move_to / jog) will see _is_at_limit()
            # is True and pre-send a sacrificial command to absorb
            # the firmware's "first command after limit-stop" drop.
        elif status == self.STATUS_FAILED:
            print("Homing FAILED. Check switch wiring.")
        else:
            print(f"Homing ended: {status}")

    # --- High-level motion ---

    def move_to(self, mm, speed_pct=20, accel_pct=10):
        """Move to absolute position in mm.

        Uses F5H coordinate-based absolute motion (manual section
        11.4). Ball screw converts mm to encoder counts.

        If a limit switch is currently closed (typically right after
        homing, or after a previous motion ended on a limit), the
        first F5 would be dropped by firmware. We absorb that drop
        by pre-sending an identical F5 — same pattern as _jog().

        Args:
            mm: Target position in millimeters.
            speed_pct: Speed as 0-100% of max RPM.
            accel_pct: Acceleration as 0-100% of max.
        """
        speed = self._pct_to_speed(speed_pct)
        accel = self._pct_to_accel(accel_pct)
        coord = self._mm_to_coord(mm)

        motion_data = self._int16_bytes(speed) + [accel] + self._int24_bytes(coord)
        print(
            f"  Moving to {mm}mm (speed={speed}RPM, accel={accel}, coord=0x{coord:06X})"
        )

        if self._is_at_limit():
            # Burn the dropped-slot F5 so the real one below takes hold.
            self._send(self.CMD_MOVE_TO, *motion_data, silent=True)

        initial = self._send(self.CMD_MOVE_TO, *motion_data)

        if initial == self.STATUS_RUNNING:
            return self._wait()

        if initial:
            print(f"[ERROR] Motor failed to start (status=0x{initial:02X})")
        else:
            print("[ERROR] No response")
        return initial

    def _jog(self, speed_rpm: int, cw: bool, accel: int = 50):
        """Start or stop continuous speed-mode jog (F6H).

        speed_rpm=0 issues a soft stop regardless of cw.

        Empirical MKS firmware quirk: when a limit switch is currently
        closed, the first F6 motion command issued in any direction is
        silently dropped — only the second consecutive F6 is honored.
        Neither status code nor an intermediate F4/stop reliably
        substitutes for that second F6.

        To avoid burning a CAN round-trip on every jog, we first query
        the IO port (0x34): only if a limit is closed do we pre-send
        a dummy F6 to absorb the dropped slot. Normal jogs (no limit
        active) issue a single F6 like before.

        Args:
            speed_rpm: Target speed in RPM (0 = stop).
            cw: True for clockwise, False for counter-clockwise.
            accel: Acceleration/deceleration value (0-255).

        Returns:
            Status byte from the (final) F6.
        """
        speed = self._clamp(speed_rpm, 0, self._max_speed_rpm)
        acc   = self._clamp(accel, 0, self._max_accel)
        byte2 = (self._jog_dir_bit if cw else 0x00) | ((speed >> 8) & 0x0F)
        byte3 = speed & 0xFF
        if speed > 0 and self._is_at_limit():
            # Burn the dropped-slot F6 so the real one below takes hold.
            self._send(self.CMD_JOG, byte2, byte3, acc, silent=True)
        return self._send(self.CMD_JOG, byte2, byte3, acc, silent=True)

    def emergency_stop(self):
        """Hard-stop the motor immediately via F7 (manual 9.2.3).

        Skips the deceleration ramp that a soft jog-stop (F6 speed=0)
        uses, so this is the fastest way to halt a runaway motor. Used as
        the fallback in the paired-group safety interlock — see
        stop_group_hard.

        Returns:
            Status byte from the motor (STATUS_SUCCESS on success), or
            None for a broadcast ID.

        Raises:
            ConnectionError: If the motor does not respond after retries.
        """
        return self._send(self.CMD_ESTOP, silent=True)

    # --- Sync helpers (multi-motor) ---

    @staticmethod
    def _sync(motors, action, barrier=None):
        """Run action(motor) on each motor in parallel, gated by a barrier.

        All threads wait at the barrier before running action(),
        so the per-motor work starts at the same instant regardless
        of thread scheduling.

        Args:
            motors: List of MKSMotor instances.
            action: Callable taking a single MKSMotor argument.
            barrier: Optional threading.Barrier. If None, one is
                created internally sized to len(motors).
        """
        if barrier is None:
            barrier = threading.Barrier(len(motors))
        threads = [
            threading.Thread(
                target=lambda m=m: (barrier.wait(), action(m))
            )
            for m in motors
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    @staticmethod
    def _run_group(motors, action, synced=False, on_first_error=None):
        """Run action(motor) on every motor in parallel, capturing errors.

        Unlike _sync, a failure in one worker thread does NOT vanish: the
        exception is recorded and returned so the caller can react (e.g.
        stop the whole paired group). This is the basis of the safety
        interlock — on a paired axis, one motor faulting must never leave
        its partner moving alone.

        Args:
            motors: List of MKSMotor instances.
            action: Callable taking a single MKSMotor argument.
            synced: When True (and more than one motor), all workers wait
                at a barrier before running action() so motion starts at
                the same instant. Leave False for stops, where each motor
                should halt as soon as possible without waiting for its
                partner's thread.
            on_first_error: Optional callable(index, exception) invoked
                from the worker thread the INSTANT the first worker raises
                — before the other workers return. This is what lets the
                interlock stop a still-running paired motor MID-move: an
                absolute (F5) move blocks in _wait() until it reaches its
                target, so reacting only after the join would fire the
                interlock too late (the healthy motor would finish its
                move first). Fired at most once per call.

        Returns:
            List of exceptions aligned with ``motors`` — None for each
            motor whose action ran without raising.
        """
        barrier = (threading.Barrier(len(motors))
                   if synced and len(motors) > 1 else None)
        errors = [None] * len(motors)
        fire_lock = threading.Lock()
        fired = [False]

        def worker(i, m):
            try:
                if barrier is not None:
                    barrier.wait()
                action(m)
            except Exception as e:  # capture so the joiner can react
                errors[i] = e
                if on_first_error is None:
                    return
                with fire_lock:
                    first = not fired[0]
                    fired[0] = True
                if first:
                    on_first_error(i, e)

        threads = [
            threading.Thread(target=worker, args=(i, m))
            for i, m in enumerate(motors)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    @staticmethod
    def _group_fault_handler(motors, kind):
        """Build an on_first_error handler that trips the interlock now.

        Stops every motor in the group (hard, retried) and fires the
        group-fault hook the instant one motor faults — without waiting
        for a partner's blocking move/home to finish.

        Args:
            motors: The group to stop.
            kind: Short label for the fault reason ("move", "home", ...).

        Returns:
            A callable(index, exception) suitable for _run_group's
            ``on_first_error``.
        """
        def handler(index, exc):
            print(f"[SAFETY] {kind} fault on a motor ({exc}) — emergency-"
                  f"stopping the group")
            MKSMotor.stop_group_hard(motors)
            _fire_group_fault(f"{kind}: a motor did not respond")
        return handler

    @staticmethod
    def stop_group_hard(motors, attempts=3):
        """Emergency-stop (F7) every motor, retrying any that error.

        Safety interlock primitive: if one motor in a paired axis faults,
        the others must not keep moving. Stops are fired in parallel with
        no barrier so a still-moving motor is halted as fast as possible,
        and any motor that errors (e.g. a transient ConnectionError on a
        flaky USB2CAN link) is retried across ``attempts`` rounds.

        Args:
            motors: List of MKSMotor instances to halt together.
            attempts: Max rounds to retry motors that did not respond.

        Returns:
            True if every motor acknowledged a stop within the attempts;
            False if at least one stayed unreachable (caller should treat
            this as a hardware-level emergency — software cannot stop it).
        """
        pending = list(motors)
        for _ in range(attempts):
            if not pending:
                break
            errors = MKSMotor._run_group(
                pending, lambda m: m.emergency_stop())
            pending = [m for m, e in zip(pending, errors) if e is not None]
        if pending:
            print(f"[SAFETY] {len(pending)} motor(s) did NOT confirm an "
                  f"emergency stop — CUT POWER if a paired axis is racking.")
        return not pending

    @staticmethod
    def move_sync(motors, moves, barrier=None):
        """Run the same move sequence on multiple motors in sync.

        Safety interlock: if any motor's move raises (e.g. a CAN
        ConnectionError mid-move), every motor in the group is
        emergency-stopped so a paired axis cannot have one side moving
        while the other has faulted.

        Args:
            motors: List of MKSMotor instances to move together.
            moves: List of argument tuples passed to move_to()
                in order.
            barrier: Accepted for backward compatibility; ignored. Sync
                start is now handled internally via _run_group.
        """
        MKSMotor._run_group(
            motors,
            lambda m: [m.move_to(*args) for args in moves],
            synced=True,
            on_first_error=MKSMotor._group_fault_handler(motors, "move"),
        )

    @staticmethod
    def home_sync(motors, direction=0x01, barrier=None):
        """Run homing on multiple motors in parallel, with the interlock.

        Homing actively sends SET_HOME / START_HOME and waits, so a CAN
        link drop surfaces as a raised ConnectionError — most commonly a
        START_HOME that gets no response, which would otherwise leave one
        Z motor stationary while its partner homes and racks the gantry.
        On any such fault, every motor in the group is emergency-stopped
        and the group-fault hook fires (so bridge.py aborts).

        Args:
            motors: List of MKSMotor instances to home together.
            direction: Forwarded to home() on each motor; the group
                shares a single homing direction.
            barrier: Accepted for backward compatibility; ignored. Sync
                start is handled internally via _run_group.
        """
        MKSMotor._run_group(
            motors, lambda m: m.home(direction=direction), synced=True,
            on_first_error=MKSMotor._group_fault_handler(motors, "home"))

    # --- High-level convenience for UI-driven control ---

    @staticmethod
    def jog_start(motors, positive, invert, speed_rpm, accel=0):
        """Jog a group of motors in a user-facing direction.

        Translates the "+ / -" convention used by the UI / scripts
        into the F6 CW/CCW bit, applying the per-axis `invert` flag
        so wiring corrections live at the call site rather than in
        the motion routine.

        Args:
            motors: List of MKSMotor instances to jog together.
            positive: True for the user-facing "+" direction.
            invert: True to flip the physical direction (axis-level
                wiring correction).
            speed_rpm: Jog speed in RPM.
            accel: Jog acceleration (0-255).
        """
        cw = positive ^ invert
        # Barrier-synced start so a paired axis moves together. If ANY
        # motor fails to receive the jog, emergency-stop the whole group
        # rather than letting the rest run on alone.
        MKSMotor._run_group(
            motors, lambda m: m._jog(speed_rpm, cw, accel), synced=True,
            on_first_error=MKSMotor._group_fault_handler(motors, "jog start"))

    @staticmethod
    def jog_stop(motors, accel=0):
        """Soft-stop a group of motors, escalating to a hard stop on fault.

        Each motor gets a soft F6 speed=0 (ramped) stop. If any motor
        fails to stop — the dangerous case, since its partner has already
        stopped and the pair would rack — every motor is escalated to an
        F7 emergency stop with retries via stop_group_hard.

        Args:
            motors: List of MKSMotor instances to stop.
            accel: Deceleration value (0-255). Higher = faster stop.
        """
        # No barrier: halt each motor as soon as possible.
        MKSMotor._run_group(
            motors, lambda m: m._jog(0, False, accel),
            on_first_error=MKSMotor._group_fault_handler(motors, "jog stop"))

    @staticmethod
    def home_xz(z_motors, x_motor, home_dir_z=0x00, home_dir_x=0x01):
        """Home the paired Z axis (in parallel) and then the X axis.

        Project-specific helper for the current XZ stage topology. Both
        axes go through home_sync (X as a one-motor group) so a CAN fault
        during homing trips the interlock — emergency-stop the group and
        fire the group-fault hook — instead of leaving one Z motor
        stationary while its partner homes and racks the gantry.

        Args:
            z_motors: List of Z-axis MKSMotor instances (paired).
            x_motor: X-axis MKSMotor instance.
            home_dir_z: 0x90 direction byte for Z homing.
            home_dir_x: 0x90 direction byte for X homing.
        """
        print("Homing Z motors...")
        MKSMotor.home_sync(z_motors, direction=home_dir_z)
        print("Z homing complete.")

        print("Homing X motor...")
        MKSMotor.home_sync([x_motor], direction=home_dir_x)
        print("X homing complete.")

