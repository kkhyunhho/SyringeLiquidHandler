"""``python -m server`` entry point for the SyringeLiquidHandler cell.

Reads a TOML config (``[pump]`` / ``[balance]`` / ``[stage]`` for the cell,
``[server]`` for host/port/log level), opens the cell once, and hands the
live app to uvicorn. Single worker — multiple workers would each try to open
the pump/balance serial handles and fight for them.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import uvicorn

from cell.balance_linear_cell import BalanceLinearCell, BalanceLinearConfig
from cell.pump_gantry_cell import Config, PumpGantryCell
from server.app import create_app


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 17054
    log_level: str = "info"


def _load(path: Path) -> tuple[Config, ServerConfig]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    pump = raw.get("pump", {})
    stage = raw.get("stage", {})
    server = raw.get("server", {})
    cell_cfg = Config(
        pump_port=pump.get("port", "1A86:7523"),
        pump_address=int(pump.get("address", 1)),
        pump_baud=int(pump.get("baud", 9600)),
        syringe_uL=int(pump.get("syringe_uL", 125)),
        pump_init_force=int(pump.get("init_force", 2)),
        motor_serial_x=stage.get("serial_x", "NTAM63XD"),
        z_coord_invert=bool(stage.get("z_coord_invert", True)),
        x_coord_invert=bool(stage.get("x_coord_invert", True)),
        home_dir_z=int(stage.get("home_dir_z", 0)),
        home_dir_x=int(stage.get("home_dir_x", 0)),
    )
    server_cfg = ServerConfig(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 17054)),
        log_level=server.get("log_level", "info"),
    )
    return cell_cfg, server_cfg


def _load_balance_linear(path: Path) -> tuple[BalanceLinearConfig, ServerConfig]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    linear = raw.get("linear", {})
    balance = raw.get("balance", {})
    server = raw.get("server", {})
    bl_cfg = BalanceLinearConfig(
        linear_port=linear.get("port", "110A:1150"),
        scale_port=balance.get("port"),
        ambient=balance.get("ambient"),
    )
    server_cfg = ServerConfig(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 17060)),  # cell4 default
        log_level=server.get("log_level", "info"),
    )
    return bl_cfg, server_cfg


def _infer_cell(path: Path) -> str:
    """Pick the cell shape from the config's tables so `--config` alone selects
    it. A ``[linear]`` (or ``[balance]``) table → ``balance_linear`` (cell4);
    otherwise ``pump_gantry`` (cell1–3). ``--cell`` overrides this."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    has_bl = "linear" in raw or "balance" in raw
    return "balance_linear" if has_bl else "pump_gantry"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slh-server")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "TOML config path. Default: $SLH_SERVER_CONFIG env var, "
            "otherwise ./server/slh.toml."
        ),
    )
    parser.add_argument(
        "--cell",
        choices=("pump_gantry", "balance_linear"),
        default=None,
        help=(
            "Cell shape to serve. Omit to auto-detect from the config: a "
            "[linear] table → 'balance_linear' (cell4), otherwise 'pump_gantry' "
            "(cell1–3). Pass explicitly only to override the inference."
        ),
    )
    args = parser.parse_args(argv)

    if args.config is not None:
        cfg_path = args.config
    elif env := os.environ.get("SLH_SERVER_CONFIG"):
        cfg_path = Path(env)
    else:
        cfg_path = Path("server/slh.toml")

    if not cfg_path.exists():
        parser.error(f"config file not found: {cfg_path}")

    cell_kind = args.cell or _infer_cell(cfg_path)
    if cell_kind == "balance_linear":
        bl_cfg, server_cfg = _load_balance_linear(cfg_path)
        factory = lambda: BalanceLinearCell.open(bl_cfg)  # noqa: E731
    else:
        cell_cfg, server_cfg = _load(cfg_path)
        factory = lambda: PumpGantryCell.open(cell_cfg)  # noqa: E731

    app = create_app(cell_factory=factory)
    uvicorn.run(
        app,
        host=server_cfg.host,
        port=server_cfg.port,
        log_level=server_cfg.log_level,
        timeout_keep_alive=120,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
