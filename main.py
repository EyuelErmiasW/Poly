#!/usr/bin/env python3
"""CLI entry point for the Polymarket trading bot."""

import argparse
import logging
import sys

from polybot.config import Config
from polybot.bot import Bot


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    sub = parser.add_subparsers(dest="command")

    # ── run (default) ─────────────────────────────────────────
    run_parser = sub.add_parser("run", help="Run the trading bot")
    run_parser.add_argument(
        "--strategy",
        choices=[
            "value_finder", "midpoint_scalper", "volume_momentum",
            "crypto_oracle", "fast_crypto", "short_scalper",
        ],
        help="Override the strategy from .env",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Force dry-run mode (no real trades)",
    )
    run_parser.add_argument(
        "--live",
        action="store_true",
        help="Force live mode (real trades — use with caution)",
    )
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle then exit",
    )
    run_parser.add_argument(
        "--interval",
        type=int,
        help="Override scan interval in seconds (default: 15 for short_scalper, 60 for others)",
    )

    # ── backtest ──────────────────────────────────────────────
    bt_parser = sub.add_parser("backtest", help="Backtest strategy on historical data")
    bt_parser.add_argument(
        "--days", type=int, default=7,
        help="Days of history to test (default: 7)",
    )
    bt_parser.add_argument(
        "--bet-size", type=float, default=1.0,
        help="Simulated bet size in USD (default: 1.0)",
    )
    bt_parser.add_argument(
        "--min-confidence", type=float, default=0.20,
        help="Minimum confidence for Up/Down trades (default: 0.20)",
    )
    bt_parser.add_argument(
        "--min-edge", type=float, default=0.03,
        help="Minimum edge for strike-price trades (default: 0.03)",
    )
    bt_parser.add_argument(
        "--max-window", type=int, default=15,
        help="Maximum Up/Down market window in minutes (default: 15)",
    )
    bt_parser.add_argument(
        "--assets", nargs="+", default=None,
        help="Filter to specific assets (e.g. BTC ETH SOL)",
    )
    bt_parser.add_argument(
        "--max-markets", type=int, default=500,
        help="Maximum number of markets to test (default: 500)",
    )
    bt_parser.add_argument(
        "--lookahead", type=int, default=5,
        help="Minutes before close to simulate Up/Down signal (default: 5)",
    )
    bt_parser.add_argument(
        "--lookahead-hours", type=float, default=12,
        help="Hours before close to simulate strike signal (default: 12)",
    )
    bt_parser.add_argument(
        "--fee-rate", type=float, default=0.0625,
        help="Polymarket taker fee rate for 15-min crypto markets (default: 0.0625 → ~1.56%% at p=0.50)",
    )
    bt_parser.add_argument(
        "--slippage", type=float, default=0.02,
        help="Entry price slippage in cents (default: 0.02)",
    )
    bt_parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging to see API responses",
    )

    args = parser.parse_args()

    # Default to "run" if no subcommand given
    if args.command is None:
        args.command = "run"
        # Re-parse with run defaults
        args.strategy = None
        args.dry_run = None
        args.live = False
        args.once = False
        args.interval = None

    cfg = Config()

    if args.command == "backtest":
        log_level = "DEBUG" if getattr(args, "debug", False) else cfg.log_level
        setup_logging(log_level)
        _run_backtest(args)
        return

    # ── run command ────────────────────────────────────────────
    if args.strategy:
        cfg.strategy = args.strategy
    if args.dry_run:
        cfg.dry_run = True
    elif args.live:
        cfg.dry_run = False

    # Short scalper benefits from faster scanning
    if args.interval is not None:
        cfg.scan_interval = args.interval
    elif cfg.strategy == "short_scalper":
        cfg.scan_interval = 15  # 15-second cycles for short markets

    # Validate
    errors = cfg.validate()
    if errors:
        for e in errors:
            print(f"Config error: {e}", file=sys.stderr)
        print("\nCopy .env.example to .env and fill in your credentials.", file=sys.stderr)
        sys.exit(1)

    setup_logging(cfg.log_level)

    if not cfg.dry_run:
        print("WARNING: Bot is in LIVE mode. Real orders will be placed.", file=sys.stderr)
        print("Press Ctrl+C within 5 seconds to abort...", file=sys.stderr)
        import time
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print("Aborted.", file=sys.stderr)
            sys.exit(0)

    bot = Bot(cfg)
    if args.once:
        bot.run_once()
    else:
        bot.run()


def _run_backtest(args) -> None:
    """Run the backtester and print results."""
    from polybot.backtester import Backtester

    print("=" * 60)
    print("  POLYMARKET CRYPTO BACKTESTER")
    print("=" * 60)
    print(f"  Days back:        {args.days}")
    print(f"  Bet size:         ${args.bet_size:.2f}")
    print(f"  Min confidence:   {args.min_confidence:.0%} (Up/Down)")
    print(f"  Min edge:         {args.min_edge:.0%} (Strike)")
    print(f"  Max window:       {args.max_window} min (Up/Down)")
    print(f"  Assets:           {args.assets or 'all'}")
    print(f"  Lookahead:        {args.lookahead} min (Up/Down) / {args.lookahead_hours}h (Strike)")
    print(f"  Max markets:      {args.max_markets}")
    print(f"  Fee rate:         {args.fee_rate} (~{args.fee_rate * 0.25:.2%} at p=0.50)")
    print(f"  Slippage:         ${args.slippage:.2f}")
    print("=" * 60)
    print()

    bt = Backtester(
        bet_size=args.bet_size,
        min_confidence=args.min_confidence,
        min_edge=args.min_edge,
        max_window_mins=args.max_window,
        lookahead_mins=args.lookahead,
        lookahead_hours=args.lookahead_hours,
        fee_rate=args.fee_rate,
        slippage=args.slippage,
    )

    result = bt.run(
        days_back=args.days,
        max_markets=args.max_markets,
        assets=args.assets,
    )

    print(result.summary())


if __name__ == "__main__":
    main()
