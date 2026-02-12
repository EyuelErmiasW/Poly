"""BTC Arbitrage strategy — exploits pricing delays on BTC 5-minute markets.

Compares real-time BTC exchange price against Polymarket contract prices
for BTC up/down markets.  When the contract price lags behind the true
probability implied by the current BTC price, the bot buys the underpriced
side.

This targets the same edge as high-frequency traders who detect pricing
delays on short-duration binary markets.
"""

from __future__ import annotations

import logging

from py_clob_client.constants import BUY

from polybot.btc_scanner import BtcOpportunity
from polybot.market_analyzer import Opportunity
from polybot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)


class BtcArb(Strategy):
    """Latency arbitrage on BTC 5-minute up/down markets."""

    name = "btc_arb"

    def __init__(self, min_edge: float = 0.05) -> None:
        self.min_edge = min_edge

    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        """Standard interface — not used for BTC arb (use evaluate_btc instead)."""
        return []

    def evaluate_btc(self, btc_opportunities: list[BtcOpportunity]) -> list[Signal]:
        """Evaluate BTC-enriched opportunities and generate trade signals.

        For each opportunity where the detected edge exceeds min_edge,
        generate a BUY signal at a price that captures most of the edge
        while still offering a good fill probability.
        """
        signals: list[Signal] = []

        for btc_opp in btc_opportunities:
            opp = btc_opp.opportunity

            # Only trade when edge is large enough
            if btc_opp.edge < self.min_edge:
                continue

            # Skip if market is about to resolve (< 10 seconds) — too risky
            if btc_opp.time_to_resolution < 10:
                continue

            # Skip if market is too far out (> 10 minutes) — less predictable
            if btc_opp.time_to_resolution > 600:
                continue

            opp.score = btc_opp.edge

            # Determine entry price: place limit slightly above best bid
            # to get quick fills while still capturing edge
            if btc_opp.edge > 0:
                # Contract is underpriced — buy it
                # Place limit at best_ask to get immediate fill on mispriced contract
                # For strong edges, we want speed over price improvement
                if btc_opp.edge > 0.10:
                    # Strong edge: buy at ask for immediate fill
                    limit_price = round(opp.best_ask, 4)
                else:
                    # Moderate edge: place between bid and ask
                    limit_price = round(opp.best_bid + opp.spread * 0.6, 4)

                # Sanity: never buy above fair value
                limit_price = min(limit_price, round(btc_opp.fair_value - 0.01, 4))
                # Ensure price stays in valid range
                limit_price = max(0.01, min(limit_price, 0.99))

                direction = "Up" if btc_opp.is_up_token else "Down"
                signals.append(
                    Signal(
                        opportunity=opp,
                        side=BUY,
                        price=limit_price,
                        size=0,  # sized by risk manager
                        reason=(
                            f"BTC ${btc_opp.current_btc_price:,.0f} vs "
                            f"threshold ${btc_opp.threshold_price:,.0f} | "
                            f"{direction} fair={btc_opp.fair_value:.3f} "
                            f"market={opp.best_ask:.3f} "
                            f"edge={btc_opp.edge:.3f} "
                            f"({btc_opp.time_to_resolution:.0f}s left)"
                        ),
                    )
                )

        signals.sort(key=lambda s: s.opportunity.score, reverse=True)
        log.info("BtcArb produced %d signals from %d opportunities", len(signals), len(btc_opportunities))
        return signals
