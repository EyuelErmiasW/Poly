"""Main bot loop — ties together scanning, strategy, and execution."""

from __future__ import annotations

import logging
import time

from polybot.client import PolyClient
from polybot.config import Config
from polybot.executor import Executor
from polybot.market_analyzer import MarketAnalyzer
from polybot.risk_manager import RiskManager
from polybot.strategies.base import Strategy
from polybot.strategies.midpoint_scalper import MidpointScalper
from polybot.strategies.value_finder import ValueFinder
from polybot.strategies.volume_momentum import VolumeMomentum

log = logging.getLogger(__name__)

STRATEGIES: dict[str, type[Strategy]] = {
    "value_finder": ValueFinder,
    "midpoint_scalper": MidpointScalper,
    "volume_momentum": VolumeMomentum,
}


def build_strategy(cfg: Config) -> Strategy:
    cls = STRATEGIES.get(cfg.strategy)
    if cls is None:
        raise ValueError(
            f"Unknown strategy '{cfg.strategy}'. "
            f"Available: {', '.join(STRATEGIES)}"
        )
    return cls(min_edge=cfg.min_edge)


class Bot:
    """The main trading bot."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = PolyClient(cfg)
        self.analyzer = MarketAnalyzer(self.client)
        self.strategy = build_strategy(cfg)
        self.risk = RiskManager(cfg)
        self.executor = Executor(self.client, self.risk, cfg)

    def run_once(self) -> None:
        """Execute a single scan-evaluate-execute cycle."""
        log.info("── Scanning markets (%s strategy) ──", self.strategy.name)
        opportunities = self.analyzer.scan()

        if not opportunities:
            log.info("No opportunities found this cycle")
            return

        signals = self.strategy.evaluate(opportunities)
        if not signals:
            log.info("Strategy produced no signals")
            return

        results = self.executor.execute(signals)
        filled = sum(1 for r in results if r["status"] in ("submitted", "dry_run"))
        log.info("Cycle complete: %d/%d signals executed", filled, len(signals))

    def run(self) -> None:
        """Run the bot in a continuous loop."""
        mode = "DRY RUN" if self.cfg.dry_run else "LIVE"
        log.info(
            "Bot started [%s] — strategy=%s, interval=%ds, max_trade=$%.2f, max_exposure=$%.2f",
            mode,
            self.strategy.name,
            self.cfg.scan_interval,
            self.cfg.max_trade_size,
            self.cfg.max_total_exposure,
        )

        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                log.info("Shutting down (keyboard interrupt)")
                break
            except Exception:
                log.error("Cycle error", exc_info=True)

            log.info("Sleeping %ds until next cycle...", self.cfg.scan_interval)
            try:
                time.sleep(self.cfg.scan_interval)
            except KeyboardInterrupt:
                log.info("Shutting down (keyboard interrupt)")
                break
