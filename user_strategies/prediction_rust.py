"""Launch the standalone Rust BTC runner through the existing module path."""

import os
import subprocess
from decimal import Decimal
from pathlib import Path


def btc(args):
    root = Path(__file__).resolve().parents[1]
    name = "prediction-market.exe" if os.name == "nt" else "prediction-market"
    binary = root / "target" / "release" / name
    if not binary.is_file():
        raise ValueError("Rust runner is not built; run cargo build --release --locked from the repository root")
    if (not args.auto_btc and not args.trade_credentials) or args.list or args.paper or args.polymarket or args.predictfun:
        raise ValueError("Rust backend supports automatic BTC checking and live trading only")
    if args.auto_btc and getattr(args, "polymarket_side", "yes") == getattr(args, "predictfun_side", "no"):
        raise ValueError("Automatic BTC checking needs opposite directions on the two venues")
    quantity = Decimal(10) if args.quantity is None else args.quantity
    if not quantity.is_finite():
        raise ValueError("Positive finite --quantity is required")
    if quantity <= 0 or (args.live and quantity > 10):
        raise ValueError("Live target quantity must be >0 and <=10")
    command = [str(binary), "btc", "--quantity", str(quantity),
               "--max-trades", str(args.max_trades), "--interval", str(args.interval),
               "--seconds", str(args.seconds)]
    for flag in ("live", "preflight_only", "trade_credentials"):
        if getattr(args, flag, False):
            command.append("--" + flag.replace("_", "-"))
    return subprocess.call(command, cwd=root)
