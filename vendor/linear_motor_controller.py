# ruff: noqa
# Vendored from coport-uni/LinearMotorController (flat module, no
# pyproject upstream). Update by re-copying LinearMotorController.py.
"""Control a Panasonic MINAS A6 servo amplifier over RS485.

Communicate using the MINAS standard serial protocol
(ENQ/EOT/ACK/NAK handshaking) at 9600 bps, 8N1.
"""

import serial
import time


class LinearMotorController:
    # Magnetic linear encoder: 1 um/pulse -> 1000 pulses/mm.
    # Adjust after empirical calibration if needed.
    pulses_per_mm = 1000

    # Speed schedule (r/min) used by move_to_mm(). The first entry
    # is the coarse approach speed; later entries shrink overshoot
    # toward tolerance_mm. Edit here to change move_to_mm speeds.
    move_to_mm_speed_schedule = [50, 10, 3, 1, 1]

    def __init__(self, port: str):
        """Initialize serial port with 8N1 MINAS standard settings."""
        self.ser = serial.Serial(
            port=port,
            baudrate=9600,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=2,
        )
        self.id = 1

        self.ENQ = 0x05  # Enquiry
        self.EOT = 0x04  # End of transmission
        self.ACK = 0x06  # Acknowledgement
        self.NAK = 0x15  # Negative acknowledgement

    def _build_command(
        self, command: int, mode: int, params: bytes = b""
    ) -> bytes:
        """Build a MINAS standard protocol data block.

        Axis is fixed to 1 (0x01).

        Block layout:
            N | 0x01 | (mode<<4)|command | params | checksum
        """
        param_count = len(params)
        mode_command = ((mode & 0x0F) << 4) | (command & 0x0F)
        block = bytes([param_count, 1, mode_command]) + params

        checksum_byte = (-sum(block)) & 0xFF

        return block + bytes([checksum_byte])

    def _extract_params(self, response: bytes) -> tuple[bytes, int]:
        """Extract parameter bytes and error code from a response."""
        param_count = response[0]
        params = response[3 : 3 + param_count]
        error_code = params[-1] if params else 0xFF

        return params, error_code

    def _send_and_receive(self, block: bytes) -> bytes | None:
        """Send a command block and return the response block.

        Execute the RS485 handshake sequence:
            1) host->amp: module_byte+ENQ, amp->host: EOT
            2) host->amp: data block,      amp->host: ACK+ENQ
            3) host->amp: module_byte+EOT, amp->host: response
            4) host->amp: ACK

        Return the raw response bytes, or None on failure.
        """
        module_byte = 0x80 | (self.id & 0x7F)
        self.ser.reset_input_buffer()
        self.ser.write(bytes([module_byte, self.ENQ]))

        start = time.time()
        eot_received = False

        while time.time() - start < 2:
            data = self.ser.read(1)

            if data and data[0] == self.EOT:
                eot_received = True

                break

        if not eot_received:
            print(" No EOT response from amplifier.")
            return None

        self.ser.write(block)

        ack_data = self.ser.read(2)
        if len(ack_data) < 1:
            print("ACK response timeout.")

            return None

        if ack_data[0] == self.NAK:
            print("Received NAK (data error).")

            return None

        if ack_data[0] != self.ACK:
            print(f"Unexpected response: 0x{ack_data[0]:02X}")

            return None

        enq_received = len(ack_data) >= 2 and ack_data[1] == self.ENQ

        if not enq_received:
            start = time.time()

            while time.time() - start < 2:
                data = self.ser.read(1)

                if data and data[0] == self.ENQ:
                    enq_received = True

                    break

        if not enq_received:
            print("ENQ wait timeout.")

            return None

        self.ser.write(bytes([0x80, self.EOT]))

        first_byte = self.ser.read(1)

        if not first_byte:
            print("Response block receive timeout.")

            return None

        param_count = first_byte[0]
        expected_remaining = param_count + 3
        remaining = self.ser.read(expected_remaining)

        if len(remaining) < expected_remaining:
            print(
                f"  Incomplete response"
                f" (expected: {expected_remaining},"
                f" received: {len(remaining)})."
            )

            return None

        response = first_byte + remaining

        if sum(response) & 0xFF != 0:
            print(f"  Checksum error (sum: 0x{sum(response) & 0xFF:02X}).")
            self.ser.write(bytes([self.NAK]))

            return None

        self.ser.write(bytes([self.ACK]))

        return response

    def read_software_version(self) -> str | None:
        """Read the amplifier software version string.

        Use command=0, mode=1. Version is BCD-encoded in
        two bytes: high=X0h, low=YZh -> "Ver.X.0YZ".
        """
        block = self._build_command(command=0, mode=1)
        response = self._send_and_receive(block)

        if response is None:
            return None

        params, error_code = self._extract_params(response)

        if error_code & 0x80:
            print(f"  Error code: 0x{error_code:02X}")
            return None

        if len(params) >= 3:
            ver_high = params[0]
            ver_low = params[1]
            major = (ver_high >> 4) & 0x0F
            minor_high = ver_high & 0x0F
            minor_low_tens = (ver_low >> 4) & 0x0F
            minor_low_ones = ver_low & 0x0F
            return f"Ver.{major}.{minor_high}{minor_low_tens}{minor_low_ones}"

        return None

    def read_model_name(self) -> str | None:
        """Read a 12-character ASCII model name from the amp.

        Use command=0, mode=5 (amp model).
        """
        block = self._build_command(command=0, mode=5)
        response = self._send_and_receive(block)
        if response is None:
            return None

        params, error_code = self._extract_params(response)

        if error_code & 0x80:
            print(f"  Error code: 0x{error_code:02X}")
            return None

        if len(params) >= 2:
            model_bytes = params[:-1]
            name = model_bytes.decode("ascii", errors="replace").rstrip(
                "\x00 *"
            )
            return name if name else None

        return None

    def read_feedback_pulse_position(self) -> int | None:
        """Read the current feedback pulse counter position.

        Use command=2, mode=2. The value represents absolute
        position from the power-on origin: negative for
        reverse, positive for forward.
        """
        block = self._build_command(command=2, mode=2)
        response = self._send_and_receive(block)
        if response is None:
            return None

        params, error_code = self._extract_params(response)
        if error_code & 0x80:
            print(f"  Error code: 0x{error_code:02X}")
            return None

        if len(params) >= 5:
            # 4-byte little-endian signed integer (L, H order)
            position = int.from_bytes(
                params[0:4], byteorder="little", signed=True
            )
            return position

        return None

    def _acquire_execution_rights(self) -> bool:
        """Acquire execution rights for parameter writes.

        Use command=1, mode=7 with param=0x01 (acquire).
        Must be called before writing parameters. Release
        with _release_execution_rights() when done.
        """
        block = self._build_command(command=1, mode=7, params=bytes([0x01]))

        response = self._send_and_receive(block)
        if response is None:
            return False

        params, error_code = self._extract_params(response)
        if error_code & 0x80:
            print(f"  Execution rights acquire failed: 0x{error_code:02X}")

            return False

        return True

    def _release_execution_rights(self) -> bool:
        """Release execution rights after parameter writes.

        Use command=1, mode=7 with param=0x00 (release).
        """
        block = self._build_command(command=1, mode=7, params=bytes([0x00]))
        response = self._send_and_receive(block)
        if response is None:
            return False

        params, error_code = self._extract_params(response)
        if error_code & 0x80:
            print(f"  Execution rights release failed: 0x{error_code:02X}")

            return False

        return True

    def _write_parameter(self, category: int, number: int, value: int) -> bool:
        """Write a single parameter value to RAM.

        Use command=7, mode=1. Value is sent as signed
        32-bit little-endian. Use mode=2 to persist to
        EEPROM instead.
        """
        value_bytes = value.to_bytes(4, byteorder="little", signed=True)
        param_data = bytes([category, number]) + value_bytes
        block = self._build_command(command=7, mode=1, params=param_data)
        response = self._send_and_receive(block)
        if response is None:
            return False

        params, error_code = self._extract_params(response)
        if error_code & 0x80:
            print(f"  Parameter write failed: 0x{error_code:02X}")

            return False

        return True

    def _read_parameter(self, category: int, number: int) -> int | None:
        """Read a single parameter value.

        Use command=7, mode=0. Return the 32-bit signed
        value, or None on error.
        """
        param_data = bytes([category, number])
        block = self._build_command(command=7, mode=0, params=param_data)
        response = self._send_and_receive(block)
        if response is None:
            return None

        params, error_code = self._extract_params(response)
        if error_code & 0x80:
            print(f"  Parameter read failed: 0x{error_code:02X}")

            return None

        if len(params) >= 5:
            value = int.from_bytes(params[0:4], byteorder="little", signed=True)

            return value

        return None

    def move_relative(
        self,
        pulse_offset: int,
        speed: int = 50,
        tolerance: int = 500,
        timeout: float = 10.0,
    ) -> int | None:
        """Move the motor by pulse_offset pulses from current position.

        Set internal speed (Pr3.04) and monitor feedback
        pulses until the target is reached within tolerance.
        Require speed control mode (Pr0.01=1) and
        SRV-ON (X4, pin 26).

        Args:
            pulse_offset -- displacement in encoder pulses
            speed -- rotation speed in r/min (1~500,
                sign auto-set)
            tolerance -- acceptable error in pulses
            timeout -- maximum wait time in seconds

        Return the final position, or None on failure.
        """
        start_pos = self.read_feedback_pulse_position()
        if start_pos is None:
            return None

        target = start_pos + pulse_offset
        direction = 1 if pulse_offset > 0 else -1
        abs_speed = min(abs(speed), 500)
        print(f"  Start={start_pos}, Target={target}")

        if not self._acquire_execution_rights():
            return None

        try:
            self._write_parameter(3, 4, direction * abs_speed)

            start_time = time.time()
            while time.time() - start_time < timeout:
                current = self.read_feedback_pulse_position()
                if current is None:
                    break

                remaining = (target - current) * direction
                # Stop when reached or passed the target.
                if remaining <= tolerance:
                    break

                time.sleep(0.01)

        finally:
            # Always stop and release, even on exceptions or Ctrl+C,
            # so a KeyboardInterrupt does not leave Pr3.04 commanding
            # motion after the script exits.
            self._write_parameter(3, 4, 0)
            self._release_execution_rights()

        time.sleep(2)
        final = self.read_feedback_pulse_position()
        print(f"  Final={final}")

        return final

    def read_position_mm(self) -> float | None:
        """Read the current position in millimeters.

        Convert the feedback pulse counter to mm using
        the class-level pulses_per_mm ratio.

        Return position in mm, or None on failure.
        """
        pulses = self.read_feedback_pulse_position()
        if pulses is None:
            return None
        return pulses / self.pulses_per_mm

    def move_relative_mm(
        self,
        distance_mm: float,
        speed: int = 50,
        tolerance_mm: float = 0.5,
        timeout: float = 10.0,
    ) -> float | None:
        """Move the motor by distance_mm millimeters.

        Convert mm to encoder pulses and delegate to
        move_relative(). Use class-level pulses_per_mm
        for the conversion.

        Args:
            distance_mm -- displacement in millimeters
            speed -- motor speed in r/min (1~500,
                sign auto-set)
            tolerance_mm -- acceptable error in mm
            timeout -- maximum wait time in seconds

        Return the final position in mm, or None on
        failure.
        """
        pulse_offset = round(distance_mm * self.pulses_per_mm)
        tolerance_pulses = round(tolerance_mm * self.pulses_per_mm)
        final_pulses = self.move_relative(
            pulse_offset,
            speed=speed,
            tolerance=tolerance_pulses,
            timeout=timeout,
        )
        if final_pulses is None:
            return None
        return final_pulses / self.pulses_per_mm

    def move_to_mm(
        self,
        target_mm: float,
        tolerance_mm: float = 0.1,
        max_iterations: int = 5,
        timeout_per_step: float = 10.0,
    ) -> float | None:
        """Move to an absolute target position in millimeters.

        Implement a software closed loop on top of
        move_relative_mm(): shrink the residual error by
        iterating moves at progressively lower speeds so
        speed-mode overshoot collapses into tolerance_mm.

        Speeds come from the class attribute
        ``move_to_mm_speed_schedule`` (default
        [50, 10, 3, 1, 1] r/min). Edit that attribute to
        retune speeds without changing call sites.

        Abort early if the residual error stops decreasing
        (convergence stalled) or max_iterations is reached.

        Args:
            target_mm -- absolute target position in mm
            tolerance_mm -- acceptable |error| in mm
            max_iterations -- correction attempts cap
            timeout_per_step -- per-move timeout in seconds

        Return the final position in mm, or None on failure.
        """
        min_pulse_step = 1

        current_mm = self.read_position_mm()
        if current_mm is None:
            return None

        error_mm = target_mm - current_mm
        print(
            f"move_to_mm: target={target_mm} mm,"
            f" start={current_mm} mm, error={error_mm:+.3f} mm"
        )
        if abs(error_mm) <= tolerance_mm:
            print("  Already within tolerance; no motion issued.")
            return current_mm

        prev_abs_error = abs(error_mm)
        schedule = self.move_to_mm_speed_schedule
        for i in range(min(max_iterations, len(schedule))):
            speed = schedule[i]
            pulse_step = abs(error_mm) * self.pulses_per_mm
            if pulse_step < min_pulse_step:
                print(
                    f"  iter {i + 1}: residual {error_mm:+.4f} mm"
                    f" < 1 pulse; stop."
                )
                break

            print(f"  iter {i + 1}: move {error_mm:+.3f} mm @ speed {speed}")
            result = self.move_relative_mm(
                error_mm,
                speed=speed,
                tolerance_mm=tolerance_mm,
                timeout=timeout_per_step,
            )
            if result is None:
                print(f"  iter {i + 1}: move_relative_mm failed.")
                return None

            current_mm = self.read_position_mm()
            if current_mm is None:
                return None
            error_mm = target_mm - current_mm
            print(
                f"  iter {i + 1}: now {current_mm} mm, error {error_mm:+.4f} mm"
            )

            if abs(error_mm) <= tolerance_mm:
                print("  Converged within tolerance.")
                return current_mm

            if abs(error_mm) >= prev_abs_error:
                print(
                    "  Residual stopped decreasing; aborting"
                    " to avoid oscillation."
                )
                return current_mm
            prev_abs_error = abs(error_mm)

        print(
            f"  Did not converge within {max_iterations} iterations;"
            f" residual {error_mm:+.4f} mm."
        )
        return current_mm


def main():
    """Run a simple motor movement test scenario."""
    serial_port = "/dev/ttyUSB0"
    test_distance_mm = 40.0
    test_speed = 100
    test_iterations = 3

    lmc = LinearMotorController(serial_port)

    model = lmc.read_model_name()
    print(f"Model name is {model}")

    version = lmc.read_software_version()
    print(f"Software version is {version}")

    position_mm = lmc.read_position_mm()
    print(f"Current position is {position_mm} mm")

    print("\n--- Motor move test (mm) ---")

    for i in range(test_iterations):
        print(f"Moving +{test_distance_mm} mm")
        lmc.move_relative_mm(test_distance_mm, speed=test_speed)

        print(f"Moving -{test_distance_mm} mm")
        lmc.move_relative_mm(-test_distance_mm, speed=test_speed)

        final_mm = lmc.read_position_mm()
        print(f"Final position: {final_mm} mm")


if __name__ == "__main__":
    main()
