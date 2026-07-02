"""Single-class driver for the Runze SY-01B Smart Syringe Pump.

Everything the controller needs — enums, dataclasses, exceptions, command
constants, frame builder/parser, the serial I/O loop, the read-only query
methods, and the diagnostic flow — lives on the `SyringePumpController`
class. Operators import a single name: `from sy01b import SyringePumpController`.

Hardware assumptions: CH340-backed USB-to-RS232 dongle, DT ASCII protocol,
9600 8N1. See [DESIGN.md](../../DESIGN.md) §4 and §7 for the protocol and
diagnostic-flow rationale.

Limitations:
- Pickling nested dataclasses is unsupported (pickle resolves classes by
  qualified name and gets confused inside other classes).
- Plunger-motion API ships ``initialize``, ``move_to_steps``, and the
  µL-volume wrappers ``aspirate_uL`` / ``dispense_uL`` alongside valve
  motion. The remaining next-milestone symbols (``abort``,
  ``set_step_mode``) are still intentionally absent and pinned by
  TestNoPlungerMotionExposed.
"""

from __future__ import annotations

import logging
import time
import tomllib
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Protocol, Self

import serial
import serial.tools.list_ports

logger = logging.getLogger("sy01b")


def _hex_preview(data: bytes, limit: int = 64) -> str:
    if len(data) <= limit:
        return data.hex()
    return data[:limit].hex() + f"... ({len(data)} bytes total)"


def _resolve_port(spec: str) -> str:
    """Resolve a port spec to a concrete device path.

    ``spec`` is one of:

    - an explicit device path (contains ``/`` or starts with ``COM``);
    - a USB ``"VID:PID"`` hex string (e.g. ``"1A86:7523"`` for the
      SY-01B's CH340), matched against attached serial ports at runtime;
    - a USB ``"VID:PID:SERIAL"`` string that additionally pins one
      specific unit by its USB serial number, for when two devices share
      the same VID:PID (e.g. two identical dongles).

    Matching by USB identity keeps the pump addressable after a
    ``/dev/ttyUSB*`` renumber or a move to a different USB socket. Use the
    serial form only when the device actually exposes a unique serial —
    cheap CH340 chips report ``None``, so the SY-01B is matched by VID:PID
    alone (it is the only CH340 on the bench).

    Args:
        spec: The configured port value (device path, ``"VID:PID"``, or
            ``"VID:PID:SERIAL"``).

    Returns:
        The concrete device path to hand to ``serial.Serial``.

    Raises:
        ValueError: ``spec`` is neither a path nor valid USB-identity hex.
        RuntimeError: the spec matched zero or several devices.
    """
    if "/" in spec or spec.upper().startswith("COM"):
        return spec
    parts = spec.split(":")
    serial_number: str | None = None
    try:
        if len(parts) == 2:
            vid, pid = int(parts[0], 16), int(parts[1], 16)
        elif len(parts) == 3:
            vid, pid = int(parts[0], 16), int(parts[1], 16)
            serial_number = parts[2]
        else:
            raise ValueError
    except ValueError as exc:
        raise ValueError(
            f"port {spec!r} is neither a device path nor VID:PID[:SERIAL] hex"
        ) from exc
    matches = [
        info.device
        for info in serial.tools.list_ports.comports()
        if info.vid == vid
        and info.pid == pid
        and (serial_number is None or info.serial_number == serial_number)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError(f"no serial device matches {spec}")
    raise RuntimeError(
        f"{spec} matches several devices {matches} — add the USB serial "
        "(VID:PID:SERIAL) or use an explicit device path"
    )


class SyringePumpController:
    """Single-class driver for the Runze SY-01B (DT protocol over CH340/RS-232)."""

    __version__: ClassVar[str] = "0.2.0.dev0"

    # ----------------------------------------------------------- transport
    class Transport(Protocol):
        """Duck-typed interface for the pyserial subset that the controller uses.

        The real ``serial.Serial`` class satisfies this Protocol structurally,
        and so does ``serial.serial_for_url('loop://')`` for tests that need
        to script byte-level replies without hardware. The Protocol exists
        for type-checker honesty; runtime composition is unchanged.
        """

        is_open: bool

        def read(self, size: int = 1, /) -> bytes: ...
        def write(self, data: bytes, /) -> int | None: ...
        def flush(self) -> None: ...
        def reset_input_buffer(self) -> None: ...
        def close(self) -> None: ...

    # ------------------------------------------------------------------ enums
    class ErrorCode(IntEnum):
        OK = 0
        INIT_FAILED = 1
        INVALID_COMMAND = 2
        INVALID_OPERAND = 3
        NOT_INITIALIZED = 7
        PLUNGER_OVERLOAD = 9
        VALVE_OVERLOAD = 10
        PLUNGER_BLOCKED_BY_BYPASS = 11
        COMMAND_OVERFLOW = 15
        UNKNOWN = 0xFF

        @classmethod
        def from_byte(cls, nibble: int) -> SyringePumpController.ErrorCode:
            try:
                return cls(nibble)
            except ValueError:
                return cls.UNKNOWN

    class StepMode(StrEnum):
        NORMAL = "N0"
        FINE = "N1"
        MICRO = "N2"

        @property
        def full_stroke_steps(self) -> int:
            return (
                12_000
                if self is SyringePumpController.StepMode.NORMAL
                else 96_000
            )

    class ValvePosition(StrEnum):
        """Non-distribution valve states. Command bytes (uppercase); ?6
        replies in lowercase.

        For the MCC-4 dual-selection valve, only INPUT and OUTPUT are
        physically meaningful; BYPASS and EXTRA are included for the
        broader non-distribution valve family.
        """

        INPUT = "I"
        OUTPUT = "O"
        BYPASS = "B"
        EXTRA = "E"

    # ------------------------------------------------------------ exceptions
    class Error(Exception):
        """Base for every error raised by the SyringePumpController class."""

    class TransportError(Error):
        """Anything wrong at the serial / framing layer."""

    class TransportTimeout(TransportError):
        def __init__(
            self, elapsed_s: float, frame_sent: bytes, partial: bytes
        ) -> None:
            super().__init__(
                f"no ETX within {elapsed_s:.3f}s; sent={frame_sent!r} partial={partial!r}"
            )
            self.elapsed_s = elapsed_s
            self.frame_sent = frame_sent
            self.partial = partial

    class TransportClosed(TransportError):
        """Operation attempted on a closed transport."""

    class ProtocolError(Error):
        """Reply bytes received but the frame is malformed."""

        def __init__(self, message: str, raw: bytes = b"") -> None:
            super().__init__(message)
            self.raw = raw

    class DeviceError(Error):
        """The pump device returned a non-OK error code in its status byte."""

        def __init__(
            self,
            error_code: SyringePumpController.ErrorCode,
            command_sent: str,
            raw_reply: bytes,
        ) -> None:
            super().__init__(
                f"{type(self).__name__}: code={int(error_code)} "
                f"cmd={command_sent!r} reply={raw_reply.hex()}"
            )
            self.error_code = error_code
            self.command_sent = command_sent
            self.raw_reply = raw_reply

    class InitFailedError(DeviceError):
        """Error 1 — initialization failed."""

    class InvalidCommandError(DeviceError):
        """Error 2."""

    class InvalidOperandError(DeviceError):
        """Error 3."""

    class NotInitializedError(DeviceError):
        """Error 7."""

    class PlungerOverloadError(DeviceError):
        """Error 9 — plunger overload; must re-initialize."""

    class ValveOverloadError(DeviceError):
        """Error 10."""

    class PlungerBlockedByBypassError(DeviceError):
        """Error 11."""

    class CommandOverflowError(DeviceError):
        """Error 15."""

    class DiagnosticError(Error):
        """Base for failures of the read-only diagnostic stage."""

    class DiagnosticTimeoutError(DiagnosticError):
        """A diagnostic probe timed out."""

    class DiagnosticGarbledReplyError(DiagnosticError):
        """A diagnostic probe got a reply that did not parse as DT."""

    class LowSupplyVoltageError(DiagnosticError):
        def __init__(self, measured_v: float, min_v: float) -> None:
            super().__init__(
                f"supply voltage {measured_v:.1f}V below floor {min_v:.1f}V"
            )
            self.measured_v = measured_v
            self.min_v = min_v

    # ------------------------------------------------------------- dataclasses
    @dataclass(frozen=True, slots=True)
    class StatusByte:
        busy: bool
        error: SyringePumpController.ErrorCode
        raw: int

        @classmethod
        def decode(cls, byte: int) -> SyringePumpController.StatusByte:
            if (byte & 0x80) != 0 or (byte & 0x40) != 0x40:
                raise SyringePumpController.ProtocolError(
                    f"status byte {byte:#04x} missing fixed bit-6 frame"
                )
            busy = (byte & 0x20) != 0
            error = SyringePumpController.ErrorCode.from_byte(byte & 0x0F)
            return cls(busy=busy, error=error, raw=byte)

        @property
        def is_ok(self) -> bool:
            return self.error is SyringePumpController.ErrorCode.OK

    @dataclass(frozen=True, slots=True)
    class Reply:
        status: SyringePumpController.StatusByte
        data: bytes

    @dataclass(frozen=True, slots=True)
    class Config:
        """Pump configuration. Use ``SyringePumpController.Config(port=..., ...)`` or ``SyringePumpController.Config.from_toml(path)``."""

        port: str
        address: int = 1
        baud: int = 9600
        syringe_uL: int = 125
        step_mode: SyringePumpController.StepMode = field(
            default_factory=lambda: SyringePumpController.StepMode.NORMAL
        )
        reply_timeout_s: float = 1.0

        def __post_init__(self) -> None:
            if not 1 <= self.address <= 15:
                raise ValueError(f"address must be 1..15, got {self.address}")
            if self.syringe_uL not in SyringePumpController.ALLOWED_SYRINGES_UL:
                raise ValueError(
                    f"syringe_uL={self.syringe_uL} not in {sorted(SyringePumpController.ALLOWED_SYRINGES_UL)}"
                )
            if self.baud not in (9600, 38400):
                raise ValueError(f"baud must be 9600 or 38400, got {self.baud}")
            if self.reply_timeout_s <= 0:
                raise ValueError(
                    f"reply_timeout_s must be positive, got {self.reply_timeout_s}"
                )

        @classmethod
        def from_toml(cls, path: Path) -> SyringePumpController.Config:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            section = data.get("pump", data)
            kwargs: dict[str, object] = {
                k: v
                for k, v in section.items()
                if k in cls.__dataclass_fields__
            }
            step = kwargs.get("step_mode")
            if isinstance(step, str):
                kwargs["step_mode"] = SyringePumpController.StepMode(step)
            return cls(**kwargs)  # type: ignore[arg-type]

    @dataclass(frozen=True, slots=True)
    class DiagnosticsReport:
        software_version: str
        serial_number: str
        config: str
        supply_volts: float
        valve_position: str
        plunger_steps: int
        pre_init_status: SyringePumpController.StatusByte
        warnings: tuple[str, ...] = field(default_factory=tuple)

        @property
        def ok_to_initialize(self) -> bool:
            return self.pre_init_status.error in {
                SyringePumpController.ErrorCode.OK,
                SyringePumpController.ErrorCode.NOT_INITIALIZED,
            }

        def render(self) -> str:
            lines = [
                "SY-01B diagnostic report",
                f"  software version : {self.software_version}",
                f"  serial number    : {self.serial_number}",
                f"  config           : {self.config}",
                f"  supply voltage   : {self.supply_volts:.1f} V",
                f"  valve position   : {self.valve_position}",
                f"  plunger position : {self.plunger_steps} steps",
                f"  pre-init status  : busy={self.pre_init_status.busy} "
                f"error={self.pre_init_status.error.name}",
                f"  ok to initialize : {self.ok_to_initialize}",
            ]
            if self.warnings:
                lines.append("  warnings:")
                lines.extend(f"    - {w}" for w in self.warnings)
            return "\n".join(lines)

    # ------------------------------------------------------ class-level constants
    ALLOWED_SYRINGES_UL: ClassVar[frozenset[int]] = frozenset(
        {25, 50, 100, 125, 250, 500, 1000, 1250, 2500, 5000}
    )
    MIN_SUPPLY_VOLTS: ClassVar[float] = 22.0
    COMMAND_BUFFER_MAX: ClassVar[int] = 255

    CMD_QUERY_STATUS: ClassVar[str] = "Q"
    CMD_QUERY_SOFTWARE_VERSION: ClassVar[str] = "?23"
    CMD_QUERY_SERIAL_NUMBER: ClassVar[str] = "?202"
    CMD_QUERY_CONFIG: ClassVar[str] = "?76"
    CMD_QUERY_SUPPLY_VOLTAGE: ClassVar[str] = "*"
    CMD_QUERY_VALVE_POSITION: ClassVar[str] = "?6"
    CMD_QUERY_PLUNGER_POSITION: ClassVar[str] = "?"

    CMD_VALVE_INPUT: ClassVar[str] = "I"
    CMD_VALVE_OUTPUT: ClassVar[str] = "O"
    # BYPASS ("B") and EXTRA ("E") flow through ValvePosition.BYPASS /
    # .EXTRA — no separate CMD_ constants. Use ValvePosition members.
    CMD_VALVE_INIT: ClassVar[str] = "w"

    _ETX: ClassVar[bytes] = b"\x03"
    _ADDR_FIRST: ClassVar[int] = ord("1")

    # Requires all DeviceError subclasses and ErrorCode defined above.
    _DEVICE_ERROR_BY_CODE: ClassVar[dict[ErrorCode, type[DeviceError]]] = {
        ErrorCode.INIT_FAILED: InitFailedError,
        ErrorCode.INVALID_COMMAND: InvalidCommandError,
        ErrorCode.INVALID_OPERAND: InvalidOperandError,
        ErrorCode.NOT_INITIALIZED: NotInitializedError,
        ErrorCode.PLUNGER_OVERLOAD: PlungerOverloadError,
        ErrorCode.VALVE_OVERLOAD: ValveOverloadError,
        ErrorCode.PLUNGER_BLOCKED_BY_BYPASS: PlungerBlockedByBypassError,
        ErrorCode.COMMAND_OVERFLOW: CommandOverflowError,
    }

    # ----------------------------------------------------------- construction
    def __init__(
        self,
        transport: SyringePumpController.Transport,
        config: SyringePumpController.Config,
    ) -> None:
        self._transport = transport
        self._config = config
        self._address = config.address
        self._reply_timeout_s = config.reply_timeout_s

    @classmethod
    def open(cls, cfg: SyringePumpController.Config) -> Self:
        device = _resolve_port(cfg.port)
        port = serial.Serial(
            port=device,
            baudrate=cfg.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=cfg.reply_timeout_s,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        port.dtr = False
        port.rts = False
        logger.debug(
            "opened %s (%s) @ %d 8N1 (DTR/RTS forced low)",
            device,
            cfg.port,
            cfg.baud,
        )
        return cls(transport=port, config=cfg)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._transport.is_open:
            self._transport.close()
            logger.debug("serial port closed")

    @property
    def address(self) -> int:
        return self._address

    @property
    def config(self) -> SyringePumpController.Config:
        """Read-only access to the operator-supplied ``Config``.

        The server's ``/v1/diagnose`` endpoint surfaces ``syringe_uL``
        and the step-mode's full-stroke step count to the firmware via
        this property so the ESP32 UI can size its volume slider
        without hard-coding a bench-specific default. ``FakePump`` in
        ``tests/server/conftest.py`` already exposes ``config``
        publicly; this property closes the asymmetry between the fake
        and the real driver.
        """
        return self._config

    # ---------------------------------------------------- pure static helpers
    @staticmethod
    def format_address(address: int) -> bytes:
        if not 1 <= address <= 15:
            raise ValueError(f"address must be in 1..15, got {address}")
        return bytes([SyringePumpController._ADDR_FIRST + address - 1])

    @staticmethod
    def build_command(
        address: int, cmds: str, *, execute: bool = False
    ) -> bytes:
        body = cmds.encode("ascii")
        if execute:
            body = body + b"R"
        if len(body) > SyringePumpController.COMMAND_BUFFER_MAX:
            raise ValueError(
                f"command body is {len(body)} bytes, "
                f"exceeds {SyringePumpController.COMMAND_BUFFER_MAX}-byte pump buffer"
            )
        return (
            b"/" + SyringePumpController.format_address(address) + body + b"\r"
        )

    @staticmethod
    def parse_reply(frame: bytes) -> SyringePumpController.Reply:
        if len(frame) < 5:
            raise SyringePumpController.ProtocolError(
                f"reply too short ({len(frame)} bytes): {frame!r}", raw=frame
            )
        if frame[0:1] != b"/":
            raise SyringePumpController.ProtocolError(
                f"reply missing leading '/': {frame!r}", raw=frame
            )
        if frame[1:2] != b"0":
            raise SyringePumpController.ProtocolError(
                f"reply master address is {frame[1:2]!r}, expected b'0'",
                raw=frame,
            )
        etx_index = frame.find(SyringePumpController._ETX, 3)
        if etx_index < 0:
            raise SyringePumpController.ProtocolError(
                f"reply missing ETX terminator: {frame!r}", raw=frame
            )
        status = SyringePumpController.StatusByte.decode(frame[2])
        data = bytes(frame[3:etx_index])
        return SyringePumpController.Reply(status=status, data=data)

    @staticmethod
    def device_error_for(
        code: SyringePumpController.ErrorCode,
    ) -> type[SyringePumpController.DeviceError]:
        return SyringePumpController._DEVICE_ERROR_BY_CODE.get(
            code, SyringePumpController.DeviceError
        )

    # --------------------------------------------------------- private I/O
    def _send_and_receive(self, frame: bytes) -> bytes:
        if not self._transport.is_open:
            raise SyringePumpController.TransportClosed(
                "serial port is not open"
            )
        logger.debug("→ %s", _hex_preview(frame))
        self._transport.reset_input_buffer()
        self._transport.write(frame)
        self._transport.flush()
        buf = bytearray()
        deadline_anchor = time.monotonic()
        while True:
            chunk = self._transport.read(64)
            if chunk:
                buf.extend(chunk)
                if SyringePumpController._ETX in buf:
                    end = buf.index(SyringePumpController._ETX) + 1
                    while end < len(buf) and buf[end] in (0x0D, 0x0A):
                        end += 1
                    # CH340 dongles occasionally emit a stray byte (0xFF, NUL) before
                    # the start-of-frame on the first reply after open. Drop it.
                    frame_start = buf.find(b"/")
                    reply = (
                        bytes(buf[frame_start:end])
                        if 0 <= frame_start < end
                        else bytes(buf[:end])
                    )
                    logger.debug("← %s", _hex_preview(reply))
                    return reply
            if time.monotonic() - deadline_anchor > self._reply_timeout_s:
                raise SyringePumpController.TransportTimeout(
                    elapsed_s=time.monotonic() - deadline_anchor,
                    frame_sent=frame,
                    partial=bytes(buf),
                )

    def _query(self, cmds: str) -> SyringePumpController.Reply:
        frame = SyringePumpController.build_command(self._address, cmds)
        return SyringePumpController.parse_reply(self._send_and_receive(frame))

    def _execute(self, cmds: str) -> SyringePumpController.Reply:
        """Send ``cmds`` with trailing R; raise a typed DeviceError on non-OK status."""
        frame = SyringePumpController.build_command(
            self._address, cmds, execute=True
        )
        raw = self._send_and_receive(frame)
        reply = SyringePumpController.parse_reply(raw)
        if reply.status.error is not SyringePumpController.ErrorCode.OK:
            err_cls = SyringePumpController.device_error_for(reply.status.error)
            raise err_cls(reply.status.error, cmds, raw)
        return reply

    def _decode_ascii(self, data: bytes, name: str) -> str:
        try:
            return data.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise SyringePumpController.ProtocolError(
                f"{name} reply is not ASCII: {data!r}"
            ) from exc

    # ----------------------------------------------------- read-only queries
    def query_status(self) -> SyringePumpController.StatusByte:
        return self._query(SyringePumpController.CMD_QUERY_STATUS).status

    def query_software_version(self) -> str:
        return self._decode_ascii(
            self._query(SyringePumpController.CMD_QUERY_SOFTWARE_VERSION).data,
            "software version",
        )

    def query_serial_number(self) -> str:
        return self._decode_ascii(
            self._query(SyringePumpController.CMD_QUERY_SERIAL_NUMBER).data,
            "serial number",
        )

    def query_config(self) -> str:
        return self._decode_ascii(
            self._query(SyringePumpController.CMD_QUERY_CONFIG).data, "config"
        )

    def query_supply_voltage_v(self) -> float:
        text = self._decode_ascii(
            self._query(SyringePumpController.CMD_QUERY_SUPPLY_VOLTAGE).data,
            "supply voltage",
        )
        try:
            return int(text) / 10.0
        except ValueError as exc:
            raise SyringePumpController.ProtocolError(
                f"supply voltage reply is not a number: {text!r}"
            ) from exc

    def query_valve_position(self) -> str:
        return self._decode_ascii(
            self._query(SyringePumpController.CMD_QUERY_VALVE_POSITION).data,
            "valve position",
        )

    def query_plunger_position(self) -> int:
        text = self._decode_ascii(
            self._query(SyringePumpController.CMD_QUERY_PLUNGER_POSITION).data,
            "plunger position",
        )
        try:
            return int(text)
        except ValueError as exc:
            raise SyringePumpController.ProtocolError(
                f"plunger position reply is not a number: {text!r}"
            ) from exc

    # ---------------------------------------------------- motion: valve
    #
    # NOTE on ``wait_until_ready`` below — intentionally retained despite
    # having no in-repo caller. The manual documents ``Q``-polling as the
    # canonical busy/ready signal in serial mode (SY01BE.pdf §6, CLAUDE.md
    # "Error model"), so we keep the method on the public API to honor that
    # contract for callers on other firmware revisions or rigs where
    # ``Q.busy`` actually clears. On the bench pump's firmware 8.33 the bit
    # is permanently latched True (LearnedPatterns E5), so production code
    # here uses position-polling helpers (``_wait_for_valve_position``,
    # ``move_to_steps``'s ``?`` poll, ``initialize``'s ``?6 != "?"`` poll)
    # instead. Removing this method would force every future caller on a
    # well-behaved firmware to reimplement the same loop — keep it.
    #
    def wait_until_ready(
        self,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.05,
        initial_settle_s: float = 0.15,
    ) -> None:
        """Poll Q until busy=False. UNRELIABLE ON FIRMWARE 8.33 — see
        ``LearnedPatterns E5``: ``Q.busy`` stays True indefinitely on the
        bench pump even when mechanically idle.

        Retained as the manual-prescribed completion signal for callers on
        other firmware revisions / rigs where ``Q.busy`` works correctly,
        or for explicit diagnostic timing. Within this codebase, prefer
        position polling (``?`` for plunger, ``?6`` for valve) instead.

        Raises TransportTimeout if ``timeout_s`` elapses with busy still
        set; raises a typed DeviceError if Q reports a non-OK error code.
        Logs at INFO if elapsed > 2.0 s.
        """
        deadline = time.monotonic() + timeout_s
        start = time.monotonic()
        if initial_settle_s > 0:
            time.sleep(initial_settle_s)
        while True:
            status = self.query_status()
            if status.error is not SyringePumpController.ErrorCode.OK:
                err_cls = SyringePumpController.device_error_for(status.error)
                raise err_cls(
                    status.error, SyringePumpController.CMD_QUERY_STATUS, b""
                )
            if not status.busy:
                elapsed = time.monotonic() - start
                if elapsed > 2.0:
                    logger.info("wait_until_ready took %.2fs", elapsed)
                return
            if time.monotonic() >= deadline:
                raise SyringePumpController.TransportTimeout(
                    elapsed_s=timeout_s,
                    frame_sent=SyringePumpController.CMD_QUERY_STATUS.encode(
                        "ascii"
                    ),
                    partial=bytes([status.raw]),
                )
            time.sleep(poll_interval_s)

    def _wait_for_valve_position(
        self,
        target: str,
        *,
        timeout_s: float = 3.0,
        poll_interval_s: float = 0.05,
    ) -> None:
        """Poll ``?6`` until it returns ``target``. Used in place of ``wait_until_ready``
        for valve moves on firmware 8.33 where Q.busy is unreliable. ``target`` should be
        a normalized ``?6`` reply (e.g. ``"3"`` for distribution port 3, ``"i"`` for a
        non-distribution input position)."""
        deadline = time.monotonic() + timeout_s
        target_norm = target.strip()
        while True:
            raw = self.query_valve_position().strip()
            if raw == target_norm:
                return
            if time.monotonic() >= deadline:
                raise SyringePumpController.TransportTimeout(
                    elapsed_s=timeout_s,
                    frame_sent=SyringePumpController.CMD_QUERY_VALVE_POSITION.encode(
                        "ascii"
                    ),
                    partial=raw.encode("ascii"),
                )
            time.sleep(poll_interval_s)

    def initialize_valve(
        self,
        *,
        home_port: int = 1,
        direction_ccw: bool = False,
        settle_timeout_s: float = 5.0,
    ) -> None:
        """Initialize the valve drive only (``w<port>,<dir>R``). Does NOT move the plunger.

        Safe for the SY-01B with a syringe that may contain liquid: only the valve actuator
        homes. After the move, polls ``?6`` until it stops returning the pre-init literal
        ``?`` (LearnedPatterns E3), confirming homing completed mechanically.
        """
        if not 1 <= home_port <= 4:
            raise ValueError(f"home_port must be 1..4, got {home_port}")
        direction = 1 if direction_ccw else 0
        self._execute(
            f"{SyringePumpController.CMD_VALVE_INIT}{home_port},{direction}"
        )
        # ?6 returns '?' until home completes; poll until it returns a real position.
        deadline = time.monotonic() + settle_timeout_s
        while True:
            raw = self.query_valve_position().strip()
            if raw and raw != "?":
                return
            if time.monotonic() >= deadline:
                raise SyringePumpController.TransportTimeout(
                    elapsed_s=settle_timeout_s,
                    frame_sent=SyringePumpController.CMD_QUERY_VALVE_POSITION.encode(
                        "ascii"
                    ),
                    partial=raw.encode("ascii"),
                )
            time.sleep(0.1)

    # NOTE on ``set_valve_position`` below — intentionally retained even
    # though no script in this repo calls it. The method is the only entry
    # point to the manual's non-distribution valve mnemonics (``I``/``O``/
    # ``B``/``E``) and to the ``ValvePosition`` enum's BYPASS / EXTRA
    # states. The bench pump reports as 4-way distribution
    # (LearnedPatterns E6), so ``move_valve_to_port(n)`` is the right
    # call *here* — but on a rig where ``?6`` returns the lowercase
    # mnemonic (true non-distribution valve), this method is the correct
    # API. Keep it on the public surface so adopting such a rig doesn't
    # require re-implementing the mnemonic path.
    #
    def set_valve_position(
        self, position: SyringePumpController.ValvePosition | str
    ) -> None:
        """Move the valve to ``position`` (I/O/B/E mnemonic — for
        non-distribution valves).

        WARNING: most SY-01B pumps are configured for distribution valves
        at the factory (``?76`` reports ``X way|...``), in which case bare
        ``I``/``O`` resolve to the default input/output port set during
        initialization (typically port 1 / port X), NOT to user-specific
        MCC-4 dual-selection states. For distribution behavior use
        ``move_valve_to_port(n)`` instead.

        Requires ``initialize_valve`` first; otherwise pump responds with
        error 7.
        """
        pos = (
            SyringePumpController.ValvePosition(position)
            if isinstance(position, str)
            else position
        )
        if pos is SyringePumpController.ValvePosition.BYPASS:
            logger.warning(
                "valve set to BYPASS — subsequent plunger moves will fail with error 11"
            )
        self._execute(pos.value)
        # Fallback: fixed sleep because target port for bare I/O depends on init config.
        time.sleep(0.7)

    def move_valve_to_port(
        self,
        port: int,
        *,
        direction_ccw: bool = False,
        settle_timeout_s: float = 3.0,
    ) -> None:
        """Move the valve to distribution port ``port`` via ``I<n>R`` (CW) or ``O<n>R``
        (CCW). Verified complete by polling ``?6`` until it reports ``str(port)``.

        Requires ``initialize_valve`` first. For MCC-4 dual-selection users: the C-1 and
        C-3 states correspond to ``port=1`` and ``port=3`` respectively on a 4-way
        distribution-configured pump (HIL 2026-05-18, firmware 8.33).
        """
        if not 1 <= port <= 16:
            raise ValueError(f"port must be 1..16, got {port}")
        cmd_letter = (
            SyringePumpController.CMD_VALVE_OUTPUT
            if direction_ccw
            else SyringePumpController.CMD_VALVE_INPUT
        )
        self._execute(f"{cmd_letter}{port}")
        self._wait_for_valve_position(str(port), timeout_s=settle_timeout_s)

    # --------------------------------------------------- motion: plunger
    def initialize(
        self,
        *,
        force: int = 0,
        ccw: bool = False,
        settle_timeout_s: float = 30.0,
    ) -> None:
        """Initialize plunger and valve (``Z<force>R`` / ``Y<force>R``).

        Plunger travels to top of stroke, backs off by the ``k`` increment,
        and sets that as position 0. Valve homes CW (``Z``) or CCW (``Y``).
        Per the manual, ``v``/``V``/``c``/``S``/``L`` reset to defaults;
        ``N`` (step mode) is preserved.

        Args:
            force: ``0`` = full force (default, ≥1 mL syringes);
                ``1`` = half (250/500 µL); ``2`` = one-third (50/100 µL);
                ``10..40`` = an S-code speed (lower = slower init).
            ccw: send ``Y`` instead of ``Z`` so the valve homes CCW.
            settle_timeout_s: max wait for completion. Full stroke at the
                default 500 pps across 12 000 half-steps ≈ 24 s; 30 s
                gives margin.

        Raises:
            InitFailedError: pump reported code 1.
            TransportTimeout: valve never settled out of the pre-init
                ``?`` state within ``settle_timeout_s``.

        Polls ``?6`` (valve position) until it stops returning the
        literal pre-init ``?`` byte. ``?`` polling on the plunger is
        not reliable here: pre-init the plunger is already at 0
        whenever a prior session left it there, so a "did it reach 0"
        check exits before mechanical homing has even started. The
        firmware sequences plunger then valve, so a real ``?6`` value
        is the unambiguous "init complete" signal (same pattern as
        ``initialize_valve``). ``Q.busy`` is unreliable on firmware
        8.33 (LearnedPatterns E5).
        """
        if force not in (0, 1, 2) and not (10 <= force <= 40):
            raise ValueError(f"force must be 0/1/2 or 10..40, got {force}")
        cmd = "Y" if ccw else "Z"
        self._execute(f"{cmd}{force}")
        deadline = time.monotonic() + settle_timeout_s
        while True:
            raw = self.query_valve_position().strip()
            if raw and raw != "?":
                return
            if time.monotonic() >= deadline:
                raise SyringePumpController.TransportTimeout(
                    elapsed_s=settle_timeout_s,
                    frame_sent=(
                        SyringePumpController.CMD_QUERY_VALVE_POSITION.encode(
                            "ascii"
                        )
                    ),
                    partial=raw.encode("ascii"),
                )
            time.sleep(0.2)

    def move_to_steps(
        self,
        steps: int,
        *,
        settle_timeout_s: float = 10.0,
        poll_interval_s: float = 0.1,
    ) -> None:
        """Move the plunger to absolute position ``steps`` (``A<steps>R``).

        Range is ``0..Config.step_mode.full_stroke_steps`` (12 000 in
        NORMAL/N0; 96 000 in FINE/N1 or MICRO/N2). Polls ``?`` until the
        reported position equals ``steps`` (LearnedPatterns E5 — Q.busy
        is unreliable on firmware 8.33).

        Requires that ``initialize()`` has run; the firmware otherwise
        reports error 7. The valve must not be in bypass — error 11.

        Args:
            steps: target absolute position, 0..full_stroke_steps.
            settle_timeout_s: max wall-clock seconds to wait. The default
                10 s covers a full stroke at the post-init top speed
                (V=4000 pps over 12 000 half-steps ≈ 3 s) with margin.
            poll_interval_s: seconds between ``?`` polls.

        Raises:
            ValueError: ``steps`` is outside the configured stroke range.
            DeviceError subclass: pump rejected the move (e.g. error 7
                if not initialized, 11 if valve is in bypass).
            TransportTimeout: the reported position never matched
                ``steps`` within ``settle_timeout_s``.
        """
        stroke = self._config.step_mode.full_stroke_steps
        if not 0 <= steps <= stroke:
            raise ValueError(
                f"steps must be 0..{stroke} for step_mode "
                f"{self._config.step_mode.name}, got {steps}"
            )
        self._execute(f"A{steps}")
        deadline = time.monotonic() + settle_timeout_s
        while True:
            pos = self.query_plunger_position()
            if pos == steps:
                return
            if time.monotonic() >= deadline:
                raise SyringePumpController.TransportTimeout(
                    elapsed_s=settle_timeout_s,
                    frame_sent=(
                        SyringePumpController.CMD_QUERY_PLUNGER_POSITION.encode(
                            "ascii"
                        )
                    ),
                    partial=str(pos).encode("ascii"),
                )
            time.sleep(poll_interval_s)

    def _uL_to_steps(self, volume_uL: float) -> int:
        """Convert a contained volume in µL to an absolute half-step target.

        Uses ``Config.syringe_uL`` as the full-stroke reference and
        ``Config.step_mode.full_stroke_steps`` as the step ceiling, rounding
        to the nearest integer. Accepts ``int`` or ``float`` (``float`` is
        the natural input on syringes whose half-volume is non-integer —
        e.g. 62.5 µL on the 125 µL bench syringe).
        """
        syringe = self._config.syringe_uL
        if not 0 <= volume_uL <= syringe:
            raise ValueError(
                f"volume_uL must be 0..{syringe} (Config.syringe_uL), "
                f"got {volume_uL}"
            )
        full_stroke = self._config.step_mode.full_stroke_steps
        return round(volume_uL / syringe * full_stroke)

    def aspirate_uL(
        self,
        target_uL: float,
        *,
        settle_timeout_s: float = 10.0,
        poll_interval_s: float = 0.1,
    ) -> None:
        """Move plunger so the syringe contains ``target_uL`` µL (absolute).

        Converts ``target_uL`` to an absolute half-step position via
        ``round(target_uL / Config.syringe_uL * full_stroke_steps)`` and
        delegates to :meth:`move_to_steps`. The pump's ``A<n>`` command is
        absolute, so the method is semantically a "set contained volume"
        operation regardless of starting position; the ``aspirate`` name
        signals the typical filling direction (current < target). Use
        :meth:`dispense_uL` when the intent is emptying.

        Args:
            target_uL: target contained volume, ``0..Config.syringe_uL``.
                ``int`` or ``float``.
            settle_timeout_s: forwarded to :meth:`move_to_steps`.
            poll_interval_s: forwarded to :meth:`move_to_steps`.

        Raises:
            ValueError: ``target_uL`` outside ``[0, Config.syringe_uL]``;
                raised before any frame is sent.
            DeviceError subclass / TransportTimeout: see
                :meth:`move_to_steps`.
        """
        self.move_to_steps(
            self._uL_to_steps(target_uL),
            settle_timeout_s=settle_timeout_s,
            poll_interval_s=poll_interval_s,
        )

    def dispense_uL(
        self,
        target_uL: float = 0,
        *,
        settle_timeout_s: float = 10.0,
        poll_interval_s: float = 0.1,
    ) -> None:
        """Move plunger so the syringe contains ``target_uL`` µL (absolute).

        Wire-level identical to :meth:`aspirate_uL` — same conversion, same
        ``A<n>R`` frame — but named ``dispense`` so the call site reads
        correctly when emptying (current > target). Default ``target_uL=0``
        is the common "fully dispense" case.

        See :meth:`aspirate_uL` for argument and error contract.
        """
        self.move_to_steps(
            self._uL_to_steps(target_uL),
            settle_timeout_s=settle_timeout_s,
            poll_interval_s=poll_interval_s,
        )

    # ---------------------------------------------------- diagnostic flow
    def diagnose(self) -> SyringePumpController.DiagnosticsReport:
        """Run the read-only commissioning probe. Never sends R/Z/Y/W."""
        logger.info("starting diagnostic probe (read-only)")

        try:
            status = self.query_status()
        except SyringePumpController.TransportTimeout as exc:
            raise SyringePumpController.DiagnosticTimeoutError(
                f"echo probe Q timed out: {exc}"
            ) from exc
        except SyringePumpController.ProtocolError as exc:
            raise SyringePumpController.DiagnosticGarbledReplyError(
                f"echo probe Q reply malformed: {exc}"
            ) from exc

        if status.error not in {
            SyringePumpController.ErrorCode.OK,
            SyringePumpController.ErrorCode.NOT_INITIALIZED,
        }:
            logger.warning(
                "pre-init status reports error %s", status.error.name
            )

        try:
            software_version = self.query_software_version()
            serial_number = self.query_serial_number()
            config = self.query_config()
            supply_volts = self.query_supply_voltage_v()
            valve_position = self.query_valve_position()
            plunger_steps = self.query_plunger_position()
        except SyringePumpController.TransportTimeout as exc:
            raise SyringePumpController.DiagnosticTimeoutError(
                f"probe timed out: {exc}"
            ) from exc
        except SyringePumpController.ProtocolError as exc:
            raise SyringePumpController.DiagnosticGarbledReplyError(
                f"probe reply malformed: {exc}"
            ) from exc

        if supply_volts < SyringePumpController.MIN_SUPPLY_VOLTS:
            raise SyringePumpController.LowSupplyVoltageError(
                measured_v=supply_volts,
                min_v=SyringePumpController.MIN_SUPPLY_VOLTS,
            )

        warnings: list[str] = []
        if valve_position.upper() == "B":
            warnings.append(
                "valve is in bypass — plunger moves will fail with error 11"
            )

        report = SyringePumpController.DiagnosticsReport(
            software_version=software_version,
            serial_number=serial_number,
            config=config,
            supply_volts=supply_volts,
            valve_position=valve_position,
            plunger_steps=plunger_steps,
            pre_init_status=status,
            warnings=tuple(warnings),
        )
        logger.info(
            "diagnostic probe complete: %s", report.render().splitlines()[0]
        )
        return report
