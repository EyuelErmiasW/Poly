"""Analyze markets and produce scored opportunities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from polybot.client import Market, OrderBookSnapshot, PolyClient

log = logging.getLogger(__name__)


@dataclass
class Opportunity:
    """A potential trade the strategy layer can act on."""

    market: Market
    token_id: str
    outcome: str
    midpoint: float
    spread: float
    best_bid: float
    best_ask: float
    score: float = 0.0  # higher = more attractive


class MarketAnalyzer:
    """Scans markets and ranks trading opportunities."""

    def __init__(self, client: PolyClient, min_liquidity: float = 500.0) -> None:
        self.client = client
        self.min_liquidity = min_liquidity

    def scan(self, limit: int = 100) -> list[Opportunity]:
        """Fetch markets, pull order books, and return scored opportunities."""
        markets = self.client.fetch_active_markets(limit=limit)
        opportunities: list[Opportunity] = []

        for market in markets:
            if market.liquidity < self.min_liquidity:
                continue

            for token in market.tokens:
                token_id = token["token_id"]
                if not token_id:
                    continue
                try:
                    book = self.client.get_order_book(token_id)
                except Exception:
                    log.warning("Failed to fetch book for %s", token_id[:12], exc_info=True)
                    continue

                if book.spread <= 0:
                    continue

                opp = Opportunity(
                    market=market,
                    token_id=token_id,
                    outcome=token.get("outcome", ""),
                    midpoint=book.midpoint,
                    spread=book.spread,
                    best_bid=book.bids[0]["price"] if book.bids else 0.0,
                    best_ask=book.asks[0]["price"] if book.asks else 1.0,
                )
                opportunities.append(opp)

        log.info("Found %d raw opportunities", len(opportunities))
        return opportunities
