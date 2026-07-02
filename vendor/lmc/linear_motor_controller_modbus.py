# ruff: noqa
# Vendored verbatim from coport-uni/LinearMotorController (LinearMotorControllerModbus.py).
# See vendor/VENDORED.md for provenance. Do not edit here; re-copy from upstream.
"""Modbus-RTU + Block Operation client for the MINAS A6 amp.

Coexists with LinearMotorController.py: that class talks the MINAS
standard protocol (Pr5.37=0); this class talks Modbus-RTU (Pr5.37=2).
The amp only speaks one protocol at a time, so the choice between
the two classes is made at amp boot via Pr5.37 + EEPROM + power
cycle.

Tested against MDDLN45SL with Pr6.28=1 (Modbus-triggered Block Op).
"""

import time

import minimalmodbus

# --- Coil addresses (single-bit R/W via Modbus FC 01/05) ---
COIL_SRV_ON = 0x0060
COIL_A_CLR = 0x0061
COIL_STB = 0x0120
COIL_HOME_VIRTUAL = 0x0122
COIL_BUSY = 0x0140
COIL_HOME_CMP = 0x0141
COIL_COIN = 0x00A0

# --- Holding register addresses (16-bit; 32-bit values use 2 regs) ---
REG_BLOCK_NO = 0x4414
REG_POSITION_ACTUAL = 0x600F
REG_ERROR_CODE = 0x6001
REG_BLOCK0_COMMAND = 0x4800
REG_BLOCK0_DATA = 0x4802

# --- Block command codes ---
CC_RELATIVE_POSITION = 0x1
CC_ABSOLUTE_POSITION = 0x2
CC_CONTINUOUS_RUN = 0x3
CC_HOMING = 0x4
CC_DECEL_STOP = 0x5
CC_SPEED_UPDATE = 0x6


def make_block_command(
    command_code: int,
    arg1: int = 0,
    arg2: int = 0,
    arg3: int = 0,
    arg4: int = 0,
    arg5: int = 0,
) -> int:
    """Pack a 32-bit Block command word.

    Layout per MINAS A6 Block-data spec section 3-2-1:
        byte 3 (MSB) = command code
        byte 2       = (arg1 << 4) | arg2
        byte 1       = (arg3 << 4) | (arg4 << 2) | arg5
        byte 0 (LSB) = reserved (0)

    arg5 MSB=0 ends the block program after this block, which is
    the safe default for single-block programs.
    """
    byte3 = command_code & 0xFF
    byte2 = ((arg1 & 0x0F) << 4) | (arg2 & 0x0F)
    byte1 = ((arg3 & 0x0F) << 4) | ((arg4 & 0x03) << 2) | (arg5 & 0x03)
    return (byte3 << 24) | (byte2 << 16) | (byte1 << 8)


class LinearMotorControllerModbus:
    """Modbus-RTU client for MINAS A6 with Block Operation enabled.

    Requires amp setup (one-time, via front panel):
        Pr5.37 = 2 (RS485 Modbus-RTU 1:N)
        Pr6.28 = 1 (Block Op, Modbus-triggered)
        Pr60.52, Pr60.53, Pr60.54 (homing speeds and accel)
        Block 0 may be pre-configured for homing or set at runtime
        with `setup_homing_block`.
    """

    # Magnetic linear encoder: 1 um/pulse -> 1000 pulses/mm.
    # Kept in sync with LinearMotorController.pulses_per_mm.
    pulses_per_mm = 1000

    def __init__(
        self,
        port: str,
        slave_id: int = 1,
        baudrate: int = 9600,
        timeout: float = 1.0,
    ):
        """Open the Modbus-RTU port at 8N1.

        Defaults match the existing standard-protocol setup so the
        same physical RS485 link works after Pr5.37 is flipped to 2.
        """
        self.client = minimalmodbus.Instrument(
            port, slave_id, mode=minimalmodbus.MODE_RTU
        )
        self.client.serial.baudrate = baudrate
        self.client.serial.bytesize = 8
        self.client.serial.parity = "N"
        self.client.serial.stopbits = 1
        self.client.serial.timeout = timeout
        self.client.clear_buffers_before_each_transaction = True

    # ----- Status reads -----

    def read_position_pulses(self) -> int:
        """Read the actual position in encoder pulses (signed 32-bit)."""
        return self.client.read_long(REG_POSITION_ACTUAL, signed=True)

    def read_position_mm(self) -> float:
        """Read the actual position converted to millimeters."""
        return self.read_position_pulses() / self.pulses_per_mm

    def read_error_code(self) -> int:
        """Read the active error number (0 if no alarm)."""
        return self.client.read_register(REG_ERROR_CODE)

    def is_busy(self) -> bool:
        """Return True while a Block Op move is in progress."""
        return bool(self.client.read_bit(COIL_BUSY))

    def is_home_complete(self) -> bool:
        """Return True after a successful homing operation."""
        return bool(self.client.read_bit(COIL_HOME_CMP))

    def is_in_position(self) -> bool:
        """Return True when the amp's COIN flag is asserted."""
        return bool(self.client.read_bit(COIL_COIN))

    # ----- Servo / alarm -----

    def servo_on(self) -> None:
        """Assert SRV-ON via Modbus coil 0060h."""
        self.client.write_bit(COIL_SRV_ON, 1)

    def servo_off(self) -> None:
        """Release SRV-ON via Modbus coil 0060h."""
        self.client.write_bit(COIL_SRV_ON, 0)

    def alarm_clear(self) -> None:
        """Pulse A-CLR for 150 ms (>=120 ms required by spec)."""
        self.client.write_bit(COIL_A_CLR, 1)
        time.sleep(0.15)
        self.client.write_bit(COIL_A_CLR, 0)

    # ----- Block runtime config and trigger -----

    def write_block(
        self, block_no: int, command_word: int, data_word: int = 0
    ) -> None:
        """Write a full Block n (command + data) at runtime.

        Use Pr56.x address layout: command @ 0x4800+4n,
        data @ 0x4802+4n.
        """
        cmd_addr = REG_BLOCK0_COMMAND + 4 * block_no
        data_addr = REG_BLOCK0_DATA + 4 * block_no
        self.client.write_long(cmd_addr, command_word, signed=False)
        self.client.write_long(data_addr, data_word, signed=True)

    def setup_homing_block(self, block_no: int = 0) -> None:
        """Configure block_no as a single-shot homing program."""
        cmd = make_block_command(CC_HOMING)
        self.write_block(block_no, cmd, data_word=0)

    def setup_absolute_position_block(
        self, block_no: int, target_pulses: int
    ) -> None:
        """Configure block_no as a single-shot absolute move."""
        cmd = make_block_command(CC_ABSOLUTE_POSITION)
        self.write_block(block_no, cmd, data_word=target_pulses)

    def setup_relative_position_block(
        self, block_no: int, distance_pulses: int
    ) -> None:
        """Configure block_no as a single-shot relative move."""
        cmd = make_block_command(CC_RELATIVE_POSITION)
        self.write_block(block_no, cmd, data_word=distance_pulses)

    def trigger_block(self, block_no: int) -> None:
        """Select block_no and pulse STB to start it.

        STB auto-clears on the amp side once the block is accepted.
        """
        self.client.write_register(REG_BLOCK_NO, block_no)
        self.client.write_bit(COIL_STB, 1)

    def wait_for_completion(
        self, timeout: float = 30.0, poll_interval: float = 0.05
    ) -> bool:
        """Poll BUSY until clear or timeout. Return True on success."""
        start = time.time()
        # Allow the BUSY flag a moment to assert before polling.
        time.sleep(poll_interval)
        while time.time() - start < timeout:
            if not self.is_busy():
                return True
            time.sleep(poll_interval)
        return False

    # ----- High-level motion -----

    def home(self, block_no: int = 0, timeout: float = 60.0) -> bool:
        """Run a homing sequence and return True on completion.

        The block is (re)written each call so that interrupted prior
        runs cannot leave stale block content active.
        """
        self.setup_homing_block(block_no)
        self.trigger_block(block_no)
        if not self.wait_for_completion(timeout):
            return False
        return self.is_home_complete()

    def move_to_mm(
        self,
        target_mm: float,
        block_no: int = 1,
        timeout: float = 30.0,
    ) -> float | None:
        """Issue an absolute Block-Op move to target_mm.

        Returns the final position in mm, or None if the move did
        not complete within the timeout.
        """
        target_pulses = round(target_mm * self.pulses_per_mm)
        self.setup_absolute_position_block(block_no, target_pulses)
        self.trigger_block(block_no)
        if not self.wait_for_completion(timeout):
            return None
        return self.read_position_mm()

    def move_relative_mm(
        self,
        distance_mm: float,
        block_no: int = 2,
        timeout: float = 30.0,
    ) -> float | None:
        """Issue a relative Block-Op move by distance_mm.

        Returns the final position in mm, or None on timeout.
        """
        distance_pulses = round(distance_mm * self.pulses_per_mm)
        self.setup_relative_position_block(block_no, distance_pulses)
        self.trigger_block(block_no)
        if not self.wait_for_completion(timeout):
            return None
        return self.read_position_mm()
