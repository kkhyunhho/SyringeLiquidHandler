"""PrecisionScaleController — SBI commands for the Sartorius Entris-II.

Single-class facade for the Sartorius Entris-II precision balance over
its USB-C virtual COM port, modelled on the SyringePumpController
pattern. SBI ASCII protocol per the Entris II Technical Note
"Commands (Data Input Format)" section.

Iteration 2 adds internal calibration with ambient forced to "very
unstable" (``Esc s3_`` + ``Esc N`` + ``Esc x0_`` with ``Esc kP``
polling) and stable-weight reads through the balance's auto-push
stream — "Approach B" in the design notes. The original read-only ID
commands (``Esc x1_``, ``Esc x2_``) from iteration 1 are unchanged.

Hardware assumptions: factory-default USB-C settings per the Entris II
BCE manual §7.3.4 DEVICE/USB — SBI mode, 9600 baud, ODD parity, 8 data
bits, 1 stop bit, no handshake.

Menu-only preconditions (cannot be set via SBI on this balance —
operator must configure on the front panel before invoking
``calibrate_internal_very_unstable``, ``read_stable_weight``, or
``stream_stable_weights``):

* ``STAB.RNG = V.FAST`` — stability range, BCE manual §7.3.1 p.18.
  Distinct from AMBIENT (which the controller drives via ``Esc N``).
* ``COM.OUTP = AUTO W/`` — automatic output after stability, BCE
  manual §7.3.6 p.22. The balance autonomously pushes each new
  stable value, which ``read_stable_weight`` and
  ``stream_stable_weights`` read passively. ``IND.AFTR`` (manual
  after stability) is also stability-gated and works with ``Esc kP``
  polling, but pairing AUTO W/ with passive read avoids the
  "device busy" beeping observed when ``Esc kP`` requests overlap
  with the auto-push stream.

The calibration polling loop is intentionally asymmetric: it still
uses ``Esc kP`` to fetch progress markers (``Cal.Run.`` / ``Cal.End``)
that AUTO W/ does not push spontaneously.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from collections.abc import Iterator
from types import TracebackType
from typing import ClassVar, NamedTuple, Self

import serial
import serial.tools.list_ports


class WeightReading(NamedTuple):
    """One parsed weight measurement from an SBI print response.

    Attributes:
        value: Signed numeric weight parsed from the SBI line.
        unit: Unit symbol exactly as the balance returned it
            (typically ``'g'`` on the BCE224I).
        raw: The full stripped SBI line for downstream inspection.
    """

    value: float
    unit: str
    raw: str


def _resolve_port(spec: str | None) -> str:
    """Resolve a port spec to a concrete device path.

    ``spec`` is one of:

    - ``None`` — auto-detect the balance by USB identity (VID:PID
      ``24BC:0010``). The Picus pipette shares the Sartorius VID
      (``24BC:2202``), so the balance is matched by **full VID:PID**,
      never the vendor ID alone.
    - an explicit device path (contains ``/`` or starts with ``COM``);
    - a ``"VID:PID"`` or ``"VID:PID:SERIAL"`` hex string matched against
      attached serial ports at runtime.

    Matching by USB identity keeps the balance addressable after a
    ``/dev/ttyACM*`` renumber or a move to a different USB socket.

    Args:
        spec: Port value (``None`` to auto-detect, a device path, or
            ``"VID:PID"`` / ``"VID:PID:SERIAL"``).

    Returns:
        The concrete device path to hand to ``serial.Serial``.

    Raises:
        ValueError: ``spec`` is neither a path nor valid USB-identity hex.
        RuntimeError: the spec matched zero or several devices.
    """
    if spec is not None and ("/" in spec or spec.upper().startswith("COM")):
        return spec
    serial_number: str | None = None
    if spec is None:
        vid = PrecisionScaleController.SARTORIUS_VID
        pid = PrecisionScaleController.BALANCE_PID
        label = "Entris-II balance (24BC:0010)"
    else:
        parts = spec.split(":")
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
                f"port {spec!r} is neither a device path nor "
                "VID:PID[:SERIAL] hex"
            ) from exc
        label = spec
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
        raise RuntimeError(f"no serial device matches {label}")
    raise RuntimeError(
        f"{label} matches several devices {matches} — add the USB serial "
        "(VID:PID:SERIAL) or use an explicit device path"
    )


class PrecisionScaleController:
    """SBI controller for the Sartorius Entris-II precision balance.

    Operators import a single name from this package::

        from entris_ii import PrecisionScaleController

        with PrecisionScaleController(port="/dev/ttyACM0") as scale:
            print(scale.get_model_number())
            print(scale.get_serial_number())

    The class wraps a ``serial.Serial`` instance and exposes the two
    read-only ID commands in this iteration; the rest of the SBI
    surface (zero, tare, print, calibrate) lands in follow-up tasks.
    """

    # Sartorius USB vendor ID. The Picus pipette shares it (24BC:2202),
    # so the balance is identified by full VID:PID, not the VID alone.
    SARTORIUS_VID: ClassVar[int] = 0x24BC
    BALANCE_PID: ClassVar[int] = 0x0010

    # SBI framing bytes (Technical Note, "Format for Control Commands").
    ESC: ClassVar[bytes] = b"\x1b"
    CR: ClassVar[bytes] = b"\r"
    LF: ClassVar[bytes] = b"\n"

    # Format-1 single/short command characters (Technical Note, commands table).
    CMD_AMBIENT_VERY_UNSTABLE: ClassVar[bytes] = b"N"
    CMD_PRINT_KEY: ClassVar[bytes] = b"kP"
    CMD_CANCEL: ClassVar[bytes] = b"s3_"

    # Format-1 zero/tare command (Technical Note, commands table:
    # "T  Zero/Tara command"). Combined zero+tare — with the current
    # pan load (e.g. an empty vial) treated as the new zero, later
    # stable reads report only the net weight added afterwards.
    CMD_TARE: ClassVar[bytes] = b"T"

    # Format-2 command characters (Technical Note, commands table).
    CMD_INTERNAL_CAL: ClassVar[bytes] = b"x0_"
    CMD_MODEL_NUMBER: ClassVar[bytes] = b"x1_"
    CMD_SERIAL_NUMBER: ClassVar[bytes] = b"x2_"

    # Note: Format-1 ``Esc Z`` ("Perform internal adjustment") only
    # opens the internal-cal menu on the BCE224I — it shows
    # ``Stat Cal.Int.`` on the display and waits for confirmation
    # rather than executing. Format-2 ``Esc x0_`` actually runs the
    # procedure, so that is what ``CMD_INTERNAL_CAL`` points to.

    # Factory-default USB-C SBI parameters (Manual §7.3.4 DEVICE/USB).
    DEFAULT_BAUDRATE: ClassVar[int] = 9600
    DEFAULT_PARITY: ClassVar[str] = serial.PARITY_ODD
    DEFAULT_BYTESIZE: ClassVar[int] = serial.EIGHTBITS
    DEFAULT_STOPBITS: ClassVar[float] = serial.STOPBITS_ONE
    DEFAULT_TIMEOUT_S: ClassVar[float] = 2.0

    # Timing knobs for the calibration polling loop and stable read.
    CAL_POLL_INTERVAL_S: ClassVar[float] = 1.0
    CAL_TIMEOUT_S: ClassVar[float] = 180.0
    STABLE_READ_TIMEOUT_S: ClassVar[float] = 30.0

    # Width (chars) of the elapsed/total progress bar rendered to
    # stderr during ``calibrate_internal_very_unstable``.
    CAL_PROGRESS_BAR_WIDTH: ClassVar[int] = 20

    # Default jitter band applied by ``stream_stable_weights``. Even
    # under Approach B (AUTO W/ + passive read) the BCE224I emits
    # near-duplicate readings that differ at the 0.001 g level during
    # a steady pan — hardware-verified 2026-05-20. Readings whose
    # absolute change versus the last emitted value falls below this
    # threshold are dropped; pass ``jitter_threshold=0`` on the call
    # to fall back to exact-float deduplication only.
    JITTER_THRESHOLD: ClassVar[float] = 0.01

    # Parse one signed decimal weight + unit anywhere in an SBI line.
    # Covers both the 16-char and 22-char (ID-coded) output formats —
    # the leading ID label (e.g. "N") never contains a sign-prefixed
    # decimal followed by a unit, so the search is unambiguous.
    _WEIGHT_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"([+-]?)\s*(\d+(?:\.\d+)?)\s+([a-zA-Z]+)"
    )

    # Fallback for ID-coded lines emitted without a trailing unit,
    # e.g. ``'G         0.0000'`` observed on the BCE224I when the
    # balance prints the gross-weight ID label but omits the unit
    # field. Matches ``<id-label> <signed-numeric>`` with optional
    # trailing whitespace; the ID label is letters/digits/``#``.
    # Used only when ``_WEIGHT_RE`` misses, so unit-suffixed lines
    # continue to take the primary path.
    _WEIGHT_RE_ID_NO_UNIT: ClassVar[re.Pattern[str]] = re.compile(
        r"^([A-Za-z][A-Za-z0-9#]*)\s+"
        r"([+-]?)\s*(\d+(?:\.\d+)?)\s*$"
    )

    # Leading markers that indicate a status (not a real weight).
    # ``Stat`` is the explicit unstable indicator; ``H``/``High`` and
    # ``L``/``Low`` flag over- and under-load. These must raise
    # ``ValueError`` even when a numeric placeholder follows them,
    # otherwise the no-unit fallback above would silently treat an
    # unstable or out-of-range reading as a valid weight.
    _STATUS_PREFIX_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:Stat|High|Low|H|L)\b"
    )

    # Error markers per Technical Note "Error Codes" tables.
    _ERROR_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"\b(?:Err\s*\d+|APP\.ERR|DIS\.ERR|PRT\.ERR)\b"
    )

    @classmethod
    def find_port(cls) -> str | None:
        """Return the detected Entris-II balance port path, or None.

        Scans attached serial ports for the balance's full USB identity
        (``SARTORIUS_VID``:``BALANCE_PID`` = ``24BC:0010``). The Picus
        pipette shares the Sartorius VID (``24BC:2202``), so matching on
        the vendor ID alone would grab the wrong device — the PID is
        required. Equivalent to ``_resolve_port(None)`` but returns None
        instead of raising when nothing matches.
        """
        for info in serial.tools.list_ports.comports():
            if info.vid == cls.SARTORIUS_VID and info.pid == cls.BALANCE_PID:
                return info.device
        return None

    def __init__(
        self,
        port: str | None = None,
        baudrate: int = DEFAULT_BAUDRATE,
        parity: str = DEFAULT_PARITY,
        bytesize: int = DEFAULT_BYTESIZE,
        stopbits: float = DEFAULT_STOPBITS,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """Configure but do not yet open the serial connection.

        Args:
            port: USB-C virtual COM port spec, resolved at ``open()`` by
                ``_resolve_port``. Accepts ``None`` (auto-detect the
                balance by USB identity ``24BC:0010``), an explicit
                device path (e.g. ``/dev/ttyACM0``), or a
                ``"VID:PID"`` / ``"VID:PID:SERIAL"`` hex string. Resolving
                by USB identity survives a ``/dev/ttyACM*`` renumber.
            baudrate: SBI baud rate; factory default 9600.
            parity: Parity bit; factory default ``PARITY_ODD``.
            bytesize: Data bits; factory default 8.
            stopbits: Stop bits; factory default 1.
            timeout: Read timeout in seconds.
        """
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.bytesize = bytesize
        self.stopbits = stopbits
        self.timeout = timeout
        self._serial: serial.Serial | None = None
        self._log = logging.getLogger(self.__class__.__name__)

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        """Open the serial connection if it is not already open."""
        if self._serial is not None and self._serial.is_open:
            return
        self._serial = serial.Serial(
            port=_resolve_port(self.port),
            baudrate=self.baudrate,
            parity=self.parity,
            bytesize=self.bytesize,
            stopbits=self.stopbits,
            timeout=self.timeout,
            rtscts=False,
            xonxoff=False,
        )

    def close(self) -> None:
        """Close the serial connection if it is currently open."""
        if self._serial is not None and self._serial.is_open:
            self._serial.close()

    def _send_command(self, payload: bytes) -> None:
        """Send one SBI command, framed as ``ESC <payload> CR LF``.

        Args:
            payload: Command character(s) without framing bytes.

        Raises:
            RuntimeError: If the port is not open.
        """
        if self._serial is None or not self._serial.is_open:
            raise RuntimeError("Serial port is not open")
        frame = self.ESC + payload + self.CR + self.LF
        self._log.debug("SBI tx: %r", frame)
        self._serial.reset_input_buffer()
        self._serial.write(frame)
        self._serial.flush()

    def _read_response(self, timeout: float | None = None) -> str:
        """Read one CR-LF terminated SBI response, stripped.

        Args:
            timeout: Optional one-shot override of the port's read
                timeout (seconds). The previous timeout is restored on
                return regardless of outcome. When ``None`` the port's
                configured timeout is used.

        Returns:
            The decoded response with trailing CR/LF and surrounding
            whitespace removed.

        Raises:
            RuntimeError: If the port is not open.
            TimeoutError: If CR-LF is not observed before the read
                window expires.
        """
        if self._serial is None or not self._serial.is_open:
            raise RuntimeError("Serial port is not open")
        if timeout is None:
            line = self._serial.read_until(self.CR + self.LF)
            effective = self.timeout
        else:
            saved = self._serial.timeout
            try:
                self._serial.timeout = timeout
                line = self._serial.read_until(self.CR + self.LF)
            finally:
                self._serial.timeout = saved
            effective = timeout
        if not line.endswith(self.CR + self.LF):
            raise TimeoutError(
                f"no CR-LF within {effective}s; partial={line!r}"
            )
        self._log.debug("SBI rx: %r", line)
        return line.rstrip(b"\r\n").decode("ascii", errors="replace").strip()

    @classmethod
    def _parse_weight_line(cls, line: str) -> WeightReading:
        """Parse one SBI print response into a ``WeightReading``.

        Two response shapes are accepted:

        * Standard 16-char and 22-char forms with a trailing unit
          (e.g. ``'+    0.0000 g'`` or ``'N       0.0000 g'``).
        * ID-coded form without a trailing unit observed on the
          BCE224I (e.g. ``'G         0.0000'``). The returned
          ``unit`` is an empty string in this case so callers can
          still tell that no unit was reported.

        Lines whose leading marker is ``Stat`` (unstable), ``H`` /
        ``High`` (overload), or ``L`` / ``Low`` (underload) raise
        ``ValueError`` even when a numeric placeholder follows them.

        Args:
            line: One stripped SBI response line.

        Returns:
            ``WeightReading(value, unit, raw=line)`` for a numeric
            weight reply.

        Raises:
            RuntimeError: If the line carries an SBI error marker
                (``Err###``, ``APP.ERR``, ``DIS.ERR``, ``PRT.ERR``).
            ValueError: If the line is not parseable as a weight —
                e.g. ``Cal.Ext.`` (cal in progress), ``Stat``
                (unstable; menu misconfigured for Approach A),
                ``High`` (overload) or ``Low`` (underload).
        """
        if cls._ERROR_RE.search(line):
            raise RuntimeError(f"balance error response: {line!r}")
        if cls._STATUS_PREFIX_RE.match(line):
            raise ValueError(
                f"non-numeric SBI response (special/unstable): {line!r}"
            )
        match = cls._WEIGHT_RE.search(line)
        if match is not None:
            sign, digits, unit = match.groups()
        else:
            id_match = cls._WEIGHT_RE_ID_NO_UNIT.match(line)
            if id_match is None:
                raise ValueError(
                    f"non-numeric SBI response (special/unstable): {line!r}"
                )
            _, sign, digits = id_match.groups()
            unit = ""
        value = float(f"{sign or '+'}{digits}")
        return WeightReading(value=value, unit=unit, raw=line)

    def get_model_number(self) -> str:
        """Return the balance model number via SBI ``Esc x1_``."""
        self._send_command(self.CMD_MODEL_NUMBER)
        return self._read_response()

    def get_serial_number(self) -> str:
        """Return the balance serial number via SBI ``Esc x2_``."""
        self._send_command(self.CMD_SERIAL_NUMBER)
        return self._read_response()

    def tare(self) -> None:
        """Zero/tare the balance via SBI ``Esc T``.

        Sends the combined zero/tare command (Technical Note, commands
        table). The current pan load — typically an empty tare vial — is
        adopted as the new zero, so subsequent stable reads report only
        the net weight added after this call.

        The command itself returns no reply. Callers that need to verify
        the post-tare baseline should follow with
        :meth:`read_stable_weight`; under ``COM.OUTP = AUTO W/`` the
        balance auto-pushes the settled zero once stability is reached.

        Raises:
            RuntimeError: If the serial port is not open.
        """
        self._send_command(self.CMD_TARE)

    def flush_pending_reads(self) -> None:
        """Discard buffered auto-push lines that have not been read yet.

        Under ``COM.OUTP = AUTO W/`` the balance emits a new value on
        every stability event, so a near-duplicate reading can sit in
        the OS receive buffer between operations. Call this right before
        a deliberate load change (e.g. a syringe-pump dispense) so the
        next :meth:`read_stable_weight` returns the freshly settled value
        instead of a stale pre-change one.

        Raises:
            RuntimeError: If the serial port is not open.
        """
        if self._serial is None or not self._serial.is_open:
            raise RuntimeError("Serial port is not open")
        self._serial.reset_input_buffer()

    # Ambient-condition filter levels, SBI ``Esc K/L/M/N`` (Technical Note,
    # commands table). Looser ("unstable") settings apply heavier filtering
    # so the balance declares stability more readily in a noisy environment.
    AMBIENT_LEVELS: ClassVar[dict[str, bytes]] = {
        "very_stable": b"K",
        "stable": b"L",
        "unstable": b"M",
        "very_unstable": b"N",
    }

    def set_ambient(self, level: str) -> None:
        """Set the ambient-condition filter via SBI ``Esc K/L/M/N``.

        ``level`` is one of ``"very_stable"`` (K), ``"stable"`` (L),
        ``"unstable"`` (M), ``"very_unstable"`` (N). The looser settings
        apply heavier filtering, so the balance declares stability even
        when the pan is disturbed by drift, vibration, or draft — useful
        when :meth:`read_stable_weight` keeps timing out because the
        balance only emits ``Stat`` (unstable) lines. This is the SBI
        complement to the menu-only ``STAB.RNG`` stability range.

        Args:
            level: One of the keys of :attr:`AMBIENT_LEVELS`.

        Raises:
            ValueError: If ``level`` is not a known ambient level.
            RuntimeError: If the serial port is not open.
        """
        try:
            payload = self.AMBIENT_LEVELS[level]
        except KeyError:
            raise ValueError(
                f"unknown ambient level {level!r}; expected one of "
                f"{sorted(self.AMBIENT_LEVELS)}"
            ) from None
        self._send_command(payload)

    def calibrate_internal_very_unstable(
        self,
        timeout: float = CAL_TIMEOUT_S,
        poll_interval: float = CAL_POLL_INTERVAL_S,
    ) -> WeightReading:
        """Run internal calibration with ambient forced to very unstable.

        Preconditions (menu-only; not reachable via SBI on this
        balance — must be set on the front panel before this call):

        * ``STAB.RNG = V.FAST`` (BCE manual §7.3.1, p.18) — fastest
          stability filter, paired with the very-unstable ambient hint
          this method forces via ``Esc N``. STAB.RNG is distinct from
          AMBIENT (which is SBI-settable via ``Esc K/L/M/N``); the SBI
          command tables in the Technical Note p.4 list no command
          for STAB.RNG.
        * ``COM.OUTP = AUTO W/`` (BCE manual §7.3.6, p.22) — see the
          module docstring for the AUTO W/ vs. IND.AFTR trade-off.
          The cal polling loop below still issues ``Esc kP`` because
          it needs the progress markers (``Cal.Run.`` / ``Cal.End``)
          that AUTO W/ does not push spontaneously; the stable-read
          methods are passive and rely on the auto-push stream.

        Sequence:
            1. ``Esc s3_`` (CANCEL) clears any leftover menu state.
            2. ``Esc N`` sets ambient conditions to "very unstable".
            3. ``Esc x0_`` triggers the internal calibration cycle.
            4. ``Esc kP`` is polled until a numeric weight response
               returns (``Cal.Run.`` / ``Cal.End`` / unit-less interim
               readings are treated as in-progress).

        The balance must carry the internal calibration weight option
        (e.g. ``BCE224I-1SKR``). The pan must be empty when this is
        called; the post-calibration reading is returned so the caller
        can verify the zero baseline.

        While polling, an elapsed/total progress bar is written to
        ``sys.stderr`` once per poll iteration and finalized with a
        newline on both the success and timeout paths.

        Args:
            timeout: Maximum seconds to wait for calibration to
                complete. Default ``CAL_TIMEOUT_S``.
            poll_interval: Seconds between ``Esc kP`` polls. Default
                ``CAL_POLL_INTERVAL_S``.

        Returns:
            The first parseable ``WeightReading`` (with unit) observed
            after the calibration finishes.

        Raises:
            TimeoutError: If no parseable weight response is observed
                within ``timeout`` seconds.
            RuntimeError: If the balance returns an error code during
                polling.
        """
        self._send_command(self.CMD_CANCEL)
        time.sleep(poll_interval)
        self._send_command(self.CMD_AMBIENT_VERY_UNSTABLE)
        self._send_command(self.CMD_INTERNAL_CAL)

        start = time.monotonic()
        deadline = start + timeout
        try:
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                self._render_cal_progress(time.monotonic() - start, timeout)
                self._send_command(self.CMD_PRINT_KEY)
                try:
                    line = self._read_response(timeout=poll_interval * 2)
                except TimeoutError:
                    continue
                try:
                    reading = self._parse_weight_line(line)
                except ValueError:
                    # Cal.Run. / Cal.End / Stat / unit-less post-cal —
                    # not done yet. Keep polling for the next response.
                    continue
                # Pin the bar to 100% before the trailing newline so
                # the final on-screen state matches the success.
                self._render_cal_progress(timeout, timeout)
                return reading
        finally:
            # Always close the carriage-return line so subsequent
            # log output starts on a fresh line — covers success,
            # timeout, and any unexpected raise from inside the loop.
            sys.stderr.write("\n")
            sys.stderr.flush()
        raise TimeoutError(
            f"internal calibration did not complete within {timeout}s"
        )

    def _render_cal_progress(
        self,
        elapsed: float,
        total: float,
    ) -> None:
        """Render the calibration progress bar to stderr in-place.

        Writes one carriage-returned line of the form
        ``  [##########..........] 45/90 s`` and flushes. The caller
        is responsible for emitting the final newline once polling
        finishes.
        """
        # Clamp so the bar never overshoots when the loop is about to
        # exit on the timeout boundary.
        clamped = max(0.0, min(elapsed, total))
        ratio = clamped / total if total > 0 else 1.0
        filled = int(ratio * self.CAL_PROGRESS_BAR_WIDTH)
        empty = self.CAL_PROGRESS_BAR_WIDTH - filled
        bar = "#" * filled + "." * empty
        sys.stderr.write(f"\r  [{bar}] {int(clamped):2d}/{int(total):2d} s")
        sys.stderr.flush()

    def read_stable_weight(
        self,
        timeout: float = STABLE_READ_TIMEOUT_S,
    ) -> WeightReading:
        """Read one auto-pushed stable weight (Approach B).

        Under Approach B the balance is configured with
        ``COM.OUTP = AUTO W/`` (BCE manual §7.3.6, p.22) and emits a
        new stable value autonomously on each stability event. The
        host does not send ``Esc kP`` — it simply reads the next
        line off the wire. This avoids the "device busy" beeping
        observed when ``Esc kP`` polling overlaps with the
        auto-push stream.

        Transient non-numeric lines (``Stat``, overload markers,
        ID-coded interim drift values) are skipped automatically;
        the method keeps reading until a parseable
        :class:`WeightReading` arrives or ``timeout`` expires.

        Args:
            timeout: Maximum wall-clock seconds to wait for the next
                parseable stable reading. Defaults to
                :attr:`STABLE_READ_TIMEOUT_S`.

        Returns:
            The first parseable :class:`WeightReading` observed.

        Raises:
            TimeoutError: If no parseable reading arrives within
                ``timeout`` seconds.
            RuntimeError: If the response carries an SBI error code
                (``Err###``, ``APP.ERR``, ``DIS.ERR``, ``PRT.ERR``).
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"no stable reading within {timeout}s under AUTO W/"
                )
            try:
                line = self._read_response(timeout=remaining)
            except TimeoutError:
                raise TimeoutError(
                    f"no stable reading within {timeout}s under AUTO W/"
                ) from None
            try:
                return self._parse_weight_line(line)
            except ValueError as exc:
                # Transient non-numeric line — keep waiting for the
                # next auto-pushed value within the remaining window.
                self._log.debug("skipping non-numeric SBI line: %s", exc)

    def stream_stable_weights(
        self,
        timeout: float = STABLE_READ_TIMEOUT_S,
        jitter_threshold: float = JITTER_THRESHOLD,
    ) -> Iterator[WeightReading]:
        """Yield each auto-pushed stable weight, with jitter filter.

        Reads the balance's AUTO W/ push stream (Approach B) directly
        — no ``Esc kP`` triggers. Cadence is data-driven: the balance
        emits one line per new stable value, so the loop blocks
        between events rather than spinning.

        One host-side filter remains: readings whose absolute change
        versus the last *yielded* value is below ``jitter_threshold``
        are dropped. Hardware verification on the BCE224I (2026-05-20)
        showed that the balance still pushes near-duplicate values at
        the 0.001 g level even under AUTO W/, so the filter is kept;
        pass ``jitter_threshold=0`` to fall back to exact-float
        deduplication only.

        :class:`TimeoutError` from :meth:`read_stable_weight` (no
        stability event in ``timeout`` seconds) is logged at debug
        level and the loop continues, so a quiet pan never ends the
        stream — only ``KeyboardInterrupt`` does.

        Args:
            timeout: Per-iteration wait budget passed to
                :meth:`read_stable_weight`.
            jitter_threshold: Inclusive lower bound on the change
                magnitude required for emission. Defaults to
                :attr:`JITTER_THRESHOLD`.

        Yields:
            :class:`WeightReading` instances that survive the filter.
        """
        last_yielded: float | None = None
        while True:
            try:
                reading = self.read_stable_weight(timeout=timeout)
            except TimeoutError:
                # Quiet pan — balance hasn't emitted within the
                # window. Keep waiting; only Ctrl-C ends the stream.
                self._log.debug(
                    "no stable reading within %ss; continuing", timeout
                )
                continue
            val = reading.value
            # Jitter filter. ``delta == 0.0`` also covers exact-float
            # duplicates when the caller opts out of the jitter band
            # with ``jitter_threshold=0``.
            if last_yielded is not None:
                delta = abs(val - last_yielded)
                if delta == 0.0 or delta < jitter_threshold:
                    continue
            yield reading
            last_yielded = val
