"""``entris-ii-measure`` CLI.

Runs calibration and stable-weight commands against a real
Sartorius Entris-II balance over USB-C SBI. Unlike
``entris_ii.cli.diagnose`` (read-only ID queries), this CLI **does**
send write/control commands: it can run an internal adjustment and
read measured weights.

Subcommands:
    cal     Run internal calibration with ambient forced to "very
            unstable" (``Esc N`` then ``Esc Z``). Pan must be empty.
    read    Read one stable weight value (``Esc kP``). Requires the
            balance printer menu set to "Manual with stability"
            (Code 3.1.1.x), per "Approach A".
    watch   Stream stable weights, printing each new value once
            (exact-float dedup). Ctrl-C to stop.

Usage::

    PYTHONPATH=src python -m entris_ii.cli.measure cal [--port PATH] [-v]
    PYTHONPATH=src python -m entris_ii.cli.measure read [--port PATH] [-v]
    PYTHONPATH=src python -m entris_ii.cli.measure watch [--port PATH] [-v]
"""

from __future__ import annotations

import argparse
import logging
import sys

from entris_ii import PrecisionScaleController, WeightReading


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="entris-ii-measure",
        description=(
            "Run calibration and stable-weight commands against a "
            "Sartorius Entris-II balance over USB-C SBI."
        ),
    )
    parser.add_argument(
        "--port",
        default=None,
        help=(
            "Serial device path (e.g., /dev/ttyACM0). "
            "Default: auto-detect by Sartorius VID 0x24bc."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log SBI tx/rx frames to stderr.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{cal,read,watch}",
    )

    subparsers.add_parser(
        "cal",
        help=(
            "Run internal calibration with ambient forced to "
            "'very unstable'. Pan must be empty."
        ),
    )
    subparsers.add_parser(
        "read",
        help=(
            "Read one stable weight value (requires "
            "'Manual with stability' menu setting)."
        ),
    )
    subparsers.add_parser(
        "watch",
        help=(
            "Stream stable weights; print each new value once. Ctrl-C to stop."
        ),
    )

    return parser


def _format(reading: WeightReading) -> str:
    return f"{reading.value:+.4f} {reading.unit}  (raw: {reading.raw!r})"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    port = args.port or PrecisionScaleController.find_port()
    if port is None:
        print(
            "error: no Sartorius device detected; pass --port",
            file=sys.stderr,
        )
        return 2

    print(f"port: {port}")
    with PrecisionScaleController(port=port) as scale:
        if args.command == "cal":
            print("running internal calibration (ambient: very unstable)…")
            reading = scale.calibrate_internal_very_unstable()
            print(f"post-cal reading: {_format(reading)}")
            return 0

        if args.command == "read":
            reading = scale.read_stable_weight()
            print(_format(reading))
            return 0

        if args.command == "watch":
            print("streaming stable weights; press Ctrl-C to stop.")
            try:
                for reading in scale.stream_stable_weights():
                    print(_format(reading), flush=True)
            except KeyboardInterrupt:
                print("\nstopped.", file=sys.stderr)
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
