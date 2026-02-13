"""Value Finder strategy — buys outcomes that look underpriced.

The idea: if the spread is wide and the midpoint is near an extreme
(close to 0 or close to 1), there may be an edge in buying the
cheap side because the market is inefficient at the tails.

This is a simple mean-reversion/value approach.  It scores each
opportunity by how far the midpoint deviates from 0.50 combined with
spread width, then generates BUY signals on outcomes priced cheaply.
"""

from __future__ import annotations

import logging

from polybot.client import BUY

from polybot.market_analyzer import Opportunity
from polybot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)

# Tunables
MIN_SPREAD = 0.005  # minimum spread to consider (half a cent)
MAX_PRICE = 0.45  # buy outcomes under 45 cents
MIN_SCORE = 0.05  # minimum score to emit a signal


class ValueFinder(Strategy):
    name = "value_finder"

    def __init__(self, min_edge: float = 0.05) -> None:
        self.min_edge = min_edge

    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        signals: list[Signal] = []

        for opp in opportunities:
            if opp.spread < MIN_SPREAD:
                continue

            # Only look at cheap outcomes (potential upside)
            if opp.best_ask > MAX_PRICE:
                continue

            # Score: combination of cheapness and spread width
            cheapness = 1.0 - opp.best_ask  # cheaper = higher score
            spread_score = min(opp.spread / 0.10, 1.0)  # wider spread = more opportunity
            score = (cheapness * 0.7) + (spread_score * 0.3)

            if score < MIN_SCORE:
                continue

            opp.score = score

            # Place a limit order just above best bid to get fills
            limit_price = round(opp.best_bid + opp.spread * 0.3, 4)

            signals.append(
                Signal(
                    opportunity=opp,
                    side=BUY,
                    price=limit_price,
                    size=0,  # sized by risk manager
                    reason=f"Cheap outcome ({opp.best_ask:.2f}), score={score:.3f}",
                )
            )

        signals.sort(key=lambda s: s.opportunity.score, reverse=True)
        log.info("ValueFinder produced %d signals", len(signals))
        return signals
