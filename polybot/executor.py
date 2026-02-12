"""Order executor — bridges signals to the Polymarket API."""

from __future__ import annotations

import logging
from typing import Any

from polybot.client import PolyClient
from polybot.config import Config
from polybot.risk_manager import RiskManager
from polybot.strategies.base import Signal

log = logging.getLogger(__name__)


class Executor:
    """Takes sized signals and submits orders to Polymarket."""

    def __init__(self, client: PolyClient, risk: RiskManager, cfg: Config) -> None:
        self.client = client
        self.risk = risk
        self.cfg = cfg

    def execute(self, signals: list[Signal]) -> list[dict[str, Any]]:
        """Size, validate, and execute a batch of signals."""
        results: list[dict[str, Any]] = []

        for signal in signals:
            sized = self.risk.size_signal(signal)
            if sized is None:
                continue

            if self.cfg.dry_run:
                log.info(
                    "[DRY RUN] Would %s %.2f shares of '%s' @ %.4f — %s",
                    sized.side,
                    sized.size,
                    sized.opportunity.outcome,
                    sized.price,
                    sized.reason,
                )
                results.append(
                    {
                        "status": "dry_run",
                        "side": sized.side,
                        "size": sized.size,
                        "price": sized.price,
                        "outcome": sized.opportunity.outcome,
                        "market": sized.opportunity.market.question,
                    }
                )
                self.risk.record_fill(sized)
                continue

            try:
                resp = self.client.place_limit_order(
                    token_id=sized.opportunity.token_id,
                    price=sized.price,
                    size=sized.size,
                    side=sized.side,
                )
                self.risk.record_fill(sized)
                results.append({"status": "submitted", "response": resp})
            except Exception:
                log.error(
                    "Order failed for %s", sized.opportunity.token_id[:12], exc_info=True
                )
                results.append({"status": "error", "token_id": sized.opportunity.token_id})

        return results
