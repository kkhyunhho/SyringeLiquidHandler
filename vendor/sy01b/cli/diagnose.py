"""`sy01b-diagnose` console script.

Refuses to emit any command that could move the plunger or the valve. The
command builders it uses are read-only constants on `SyringePumpController`; no code path
inside this script constructs a frame with a trailing `R`.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from sy01b import SyringePumpController


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sy01b-diagnose",
        description="Read-only commissioning probe for the Runze SY-01B pump. Never moves anything.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to a TOML config file. If omitted, pass --port and the other flags.",
    )
    parser.add_argument(
        "--port", help="Serial port path (e.g. /dev/ttyUSB1). Overrides config."
    )
    parser.add_argument(
        "--address", type=int, help="Pump address 1..15. Overrides config."
    )
    parser.add_argument(
        "--baud", type=int, help="Baud rate (9600 or 38400). Overrides config."
    )
    parser.add_argument(
        "--syringe-uL",
        type=int,
        dest="syringe_uL",
        help="Syringe size in microliters. Overrides config.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Log frame-level send/receive to stderr.",
    )
    return parser


def _resolve_config(args: argparse.Namespace) -> SyringePumpController.Config:
    overrides: dict[str, object] = {
        k: v
        for k, v in {
            "port": args.port,
            "address": args.address,
            "baud": args.baud,
            "syringe_uL": args.syringe_uL,
        }.items()
        if v is not None
    }

    if args.config is not None:
        cfg = SyringePumpController.Config.from_toml(args.config)
        if overrides:
            cfg = dataclasses.replace(cfg, **overrides)  # type: ignore[arg-type]
        return cfg

    if "port" not in overrides:
        raise SystemExit("error: either --config or --port is required")

    return SyringePumpController.Config(**overrides)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    cfg = _resolve_config(args)

    try:
        with SyringePumpController.open(cfg) as pump:
            report = pump.diagnose()
    except SyringePumpController.DiagnosticError as exc:
        print(f"DIAGNOSTIC FAILED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 3

    print(report.render())
    return 0 if report.ok_to_initialize else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
