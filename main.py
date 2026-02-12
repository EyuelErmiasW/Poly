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
    parser.add_argument(
        "--strategy",
        choices=["value_finder", "midpoint_scalper", "volume_momentum"],
        help="Override the strategy from .env",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Force dry-run mode (no real trades)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Force live mode (real trades — use with caution)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle then exit",
    )
    args = parser.parse_args()

    cfg = Config()

    # CLI overrides
    if args.strategy:
        cfg.strategy = args.strategy
    if args.dry_run:
        cfg.dry_run = True
    elif args.live:
        cfg.dry_run = False

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


if __name__ == "__main__":
    main()
