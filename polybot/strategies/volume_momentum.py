"""Volume Momentum strategy — buys outcomes on high-volume markets.

Hypothesis: markets with high volume relative to their price have
informational momentum.  If a lot of money is flowing in while the
price is still moderate, the market may be underpriced.
"""

from __future__ import annotations

import logging

from py_clob_client.constants import BUY

from polybot.market_analyzer import Opportunity
from polybot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)

MIN_VOLUME = 5_000.0
MIN_MIDPOINT = 0.15
MAX_MIDPOINT = 0.85


class VolumeMomentum(Strategy):
    name = "volume_momentum"

    def __init__(self, min_edge: float = 0.05) -> None:
        self.min_edge = min_edge

    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        signals: list[Signal] = []

        for opp in opportunities:
            volume = opp.market.volume
            if volume < MIN_VOLUME:
                continue
            if not (MIN_MIDPOINT < opp.midpoint < MAX_MIDPOINT):
                continue
            if opp.spread <= 0:
                continue

            # Score by volume-to-price ratio (normalized)
            vol_score = min(volume / 100_000, 1.0)
            price_discount = 1.0 - opp.midpoint
            score = (vol_score * 0.6) + (price_discount * 0.4)

            opp.score = score

            # Buy near best ask for momentum plays
            limit_price = round(opp.best_ask - opp.spread * 0.1, 4)

            signals.append(
                Signal(
                    opportunity=opp,
                    side=BUY,
                    price=limit_price,
                    size=0,
                    reason=f"High volume ({volume:,.0f}), score={score:.3f}",
                )
            )

        signals.sort(key=lambda s: s.opportunity.score, reverse=True)
        log.info("VolumeMomentum produced %d signals", len(signals))
        return signals
