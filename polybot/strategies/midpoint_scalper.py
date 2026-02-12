"""Midpoint Scalper strategy — captures spread by placing limit orders.

Places both a BUY below midpoint and a SELL above midpoint, hoping to
capture the spread as profit.  Works best on liquid markets with a
meaningful spread.
"""

from __future__ import annotations

import logging

from py_clob_client.constants import BUY, SELL

from polybot.market_analyzer import Opportunity
from polybot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)

MIN_SPREAD = 0.03  # need enough room to profit
MIN_MIDPOINT = 0.10  # avoid near-zero outcomes
MAX_MIDPOINT = 0.90  # avoid near-certain outcomes


class MidpointScalper(Strategy):
    name = "midpoint_scalper"

    def __init__(self, min_edge: float = 0.05) -> None:
        self.min_edge = min_edge

    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        signals: list[Signal] = []

        for opp in opportunities:
            if opp.spread < MIN_SPREAD:
                continue
            if not (MIN_MIDPOINT < opp.midpoint < MAX_MIDPOINT):
                continue

            opp.score = opp.spread  # wider spread = more profit potential

            offset = opp.spread * 0.25

            # BUY below midpoint
            buy_price = round(opp.midpoint - offset, 4)
            signals.append(
                Signal(
                    opportunity=opp,
                    side=BUY,
                    price=buy_price,
                    size=0,
                    reason=f"Scalp BUY at {buy_price:.4f} (mid={opp.midpoint:.4f})",
                )
            )

            # SELL above midpoint
            sell_price = round(opp.midpoint + offset, 4)
            signals.append(
                Signal(
                    opportunity=opp,
                    side=SELL,
                    price=sell_price,
                    size=0,
                    reason=f"Scalp SELL at {sell_price:.4f} (mid={opp.midpoint:.4f})",
                )
            )

        log.info("MidpointScalper produced %d signals", len(signals))
        return signals
