"""``entris-ii-diagnose`` CLI.

Read-only diagnostic probe for the Sartorius Entris-II balance over
USB-C SBI. Sends only the two read-only ID queries (``Esc x1_`` and
``Esc x2_``); never emits a zero, tare, print, or calibration command.

Usage::

    PYTHONPATH=src python -m entris_ii.cli.diagnose [--port PATH] [-v]
"""

from __future__ import annotations

import argparse
import logging
import sys

from entris_ii import PrecisionScaleController


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="entris-ii-diagnose",
        description=(
            "Read-only diagnostic probe for the Sartorius Entris-II "
            "balance over USB-C SBI. Never moves or zeroes the scale."
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
    return parser


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
        print(f"model_number:  {scale.get_model_number()!r}")
        print(f"serial_number: {scale.get_serial_number()!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
