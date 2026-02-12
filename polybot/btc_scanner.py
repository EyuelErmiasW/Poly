"""Scanner specialized for BTC 5-minute up/down markets on Polymarket."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from polybot.btc_feed import BtcFeed, BtcSnapshot
from polybot.client import Market, PolyClient
from polybot.market_analyzer import Opportunity

log = logging.getLogger(__name__)

# Patterns to extract threshold price from market questions.
# Examples:
#   "Will BTC be above $97,000 at 5:00 PM UTC?"
#   "BTC above $97,500.00?"
#   "Will the price of BTC be above $97000 at 17:00 UTC on June 12?"
THRESHOLD_PATTERNS = [
    re.compile(r"\$([0-9,]+(?:\.[0-9]+)?)", re.IGNORECASE),
]

# Patterns to identify BTC up/down markets
BTC_MARKET_PATTERNS = [
    re.compile(r"\bBTC\b", re.IGNORECASE),
    re.compile(r"\bbitcoin\b", re.IGNORECASE),
]

# Slug patterns for BTC 5-minute markets
BTC_5M_SLUG = re.compile(r"btc[-_]?updown[-_]?5m", re.IGNORECASE)


@dataclass
class BtcOpportunity:
    """An opportunity enriched with BTC-specific data."""

    opportunity: Opportunity
    threshold_price: float
    current_btc_price: float
    time_to_resolution: float  # seconds
    fair_value: float  # estimated fair probability for the "Up" outcome
    edge: float  # fair_value - market_price (positive = underpriced)
    is_up_token: bool  # True if this token represents the "Up" outcome


class BtcScanner:
    """Discovers and enriches BTC 5-minute up/down markets."""

    def __init__(self, client: PolyClient, btc_feed: BtcFeed) -> None:
        self.client = client
        self.btc_feed = btc_feed

    def scan(self) -> list[BtcOpportunity]:
        """Fetch BTC markets, enrich with price data, return opportunities."""
        btc_snap = self.btc_feed.get_price()
        log.info("Current BTC price: $%.2f", btc_snap.price)

        markets = self._fetch_btc_markets()
        if not markets:
            log.info("No active BTC 5m markets found")
            return []

        opportunities: list[BtcOpportunity] = []
        for market in markets:
            opps = self._analyze_market(market, btc_snap)
            opportunities.extend(opps)

        opportunities.sort(key=lambda o: abs(o.edge), reverse=True)
        log.info(
            "BTC scanner found %d opportunities across %d markets",
            len(opportunities),
            len(markets),
        )
        return opportunities

    def _fetch_btc_markets(self) -> list[Market]:
        """Fetch markets from Gamma API, filtering for BTC up/down markets."""
        all_markets = self.client.fetch_active_markets(limit=200)
        btc_markets = []

        for market in all_markets:
            if not self._is_btc_5m_market(market):
                continue
            btc_markets.append(market)

        log.info("Found %d BTC 5m markets out of %d total", len(btc_markets), len(all_markets))
        return btc_markets

    def _is_btc_5m_market(self, market: Market) -> bool:
        """Check if a market is a BTC 5-minute up/down market."""
        q = market.question.lower()

        # Must mention BTC/Bitcoin
        has_btc = any(p.search(market.question) for p in BTC_MARKET_PATTERNS)
        if not has_btc:
            return False

        # Must be an up/down style market (above/below/over/under)
        directional = any(
            word in q for word in ("above", "below", "over", "under", "up", "down", "higher", "lower")
        )
        if not directional:
            return False

        # Should have a dollar threshold
        has_threshold = any(p.search(market.question) for p in THRESHOLD_PATTERNS)
        if not has_threshold:
            return False

        # Prefer markets with short timeframes (check slug or question for "5m")
        is_5m = "5m" in q or "5 min" in q or "five min" in q
        # Also check condition_id or end_date for short-duration markets
        if market.end_date:
            try:
                end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                remaining = (end - datetime.now(timezone.utc)).total_seconds()
                # Accept markets resolving within 30 minutes
                if remaining > 1800:
                    return False
                if remaining < 0:
                    return False
            except (ValueError, TypeError):
                pass

        return True

    def _parse_threshold(self, question: str) -> float | None:
        """Extract the dollar threshold from a market question."""
        for pattern in THRESHOLD_PATTERNS:
            match = pattern.search(question)
            if match:
                price_str = match.group(1).replace(",", "")
                try:
                    return float(price_str)
                except ValueError:
                    continue
        return None

    def _parse_resolution_time(self, market: Market) -> float | None:
        """Estimate seconds until market resolution."""
        if market.end_date:
            try:
                end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                remaining = (end - datetime.now(timezone.utc)).total_seconds()
                return max(remaining, 0)
            except (ValueError, TypeError):
                pass
        # Default: assume 5 minutes if we can't parse
        return 300.0

    def _is_up_token(self, token: dict[str, Any]) -> bool:
        """Determine if a token represents the 'Up'/'Yes' outcome."""
        outcome = token.get("outcome", "").lower()
        return outcome in ("yes", "up", "above", "over", "higher")

    def _analyze_market(
        self, market: Market, btc_snap: BtcSnapshot
    ) -> list[BtcOpportunity]:
        """Analyze a single BTC market and return enriched opportunities."""
        threshold = self._parse_threshold(market.question)
        if threshold is None:
            log.debug("Cannot parse threshold from: %s", market.question)
            return []

        time_remaining = self._parse_resolution_time(market)
        if time_remaining is None or time_remaining <= 0:
            return []

        fair_up = self._estimate_fair_value(
            current_price=btc_snap.price,
            threshold=threshold,
            time_remaining=time_remaining,
            annual_vol=btc_snap.volatility_5m,
        )

        opportunities: list[BtcOpportunity] = []
        for token in market.tokens:
            token_id = token.get("token_id", "")
            if not token_id:
                continue

            try:
                book = self.client.get_order_book(token_id)
            except Exception:
                log.warning("Failed to fetch book for %s", token_id[:12], exc_info=True)
                continue

            if book.spread <= 0:
                continue

            is_up = self._is_up_token(token)
            fair = fair_up if is_up else (1.0 - fair_up)

            best_bid = book.bids[0]["price"] if book.bids else 0.0
            best_ask = book.asks[0]["price"] if book.asks else 1.0

            # Edge for buying: fair value vs. what we'd pay (best ask)
            buy_edge = fair - best_ask
            # Edge for selling: what we'd receive (best bid) vs. fair value
            sell_edge = best_bid - fair

            edge = max(buy_edge, sell_edge)
            opp = Opportunity(
                market=market,
                token_id=token_id,
                outcome=token.get("outcome", ""),
                midpoint=book.midpoint,
                spread=book.spread,
                best_bid=best_bid,
                best_ask=best_ask,
                score=abs(edge),
            )

            btc_opp = BtcOpportunity(
                opportunity=opp,
                threshold_price=threshold,
                current_btc_price=btc_snap.price,
                time_to_resolution=time_remaining,
                fair_value=fair,
                edge=edge,
                is_up_token=is_up,
            )
            opportunities.append(btc_opp)

        return opportunities

    @staticmethod
    def _estimate_fair_value(
        current_price: float,
        threshold: float,
        time_remaining: float,
        annual_vol: float,
    ) -> float:
        """Estimate the fair probability that BTC will be above threshold at resolution.

        Uses a log-normal model with the error function (no scipy needed).
        Returns the probability of the "Up" outcome.
        """
        import math

        if threshold <= 0 or current_price <= 0:
            return 0.5

        # Convert annual volatility to the volatility for time_remaining
        # time_remaining is in seconds; annualize factor
        years = time_remaining / (365.25 * 24 * 3600)
        if years <= 0:
            # No time left: outcome is determined by current price
            return 1.0 if current_price >= threshold else 0.0

        sigma = annual_vol * math.sqrt(years)
        if sigma <= 0:
            return 1.0 if current_price >= threshold else 0.0

        # d2 from Black-Scholes (assuming zero drift for short timeframes)
        d = (math.log(current_price / threshold) + 0.5 * sigma ** 2) / sigma

        # Normal CDF via error function
        prob_up = 0.5 * (1.0 + math.erf(d / math.sqrt(2)))

        # Clamp to [0.01, 0.99] to avoid certainties
        return max(0.01, min(prob_up, 0.99))
