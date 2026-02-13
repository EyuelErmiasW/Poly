"""Crypto Oracle strategy — trades short-term Bitcoin/ETH price markets
using external price feeds (Binance) to find mispricings.

Approach (inspired by reference market-maker bot):
  1. Find Polymarket markets like "Bitcoin above $X on <date>"
  2. Fetch real-time BTC price + recent momentum from Binance
  3. Estimate probability of hitting the strike using price distance + volatility
  4. Compare our estimate to Polymarket's price — trade when gap exceeds threshold
  5. BUY underpriced outcomes, skip overpriced ones
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

from polybot.client import BUY, SELL
from polybot.market_analyzer import Opportunity
from polybot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)

# Binance public API (no auth needed)
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Minimum discrepancy between our estimate and market price to trade
MIN_DISCREPANCY = 0.03  # 3 cents


def fetch_crypto_price(symbol: str = "BTCUSDT") -> float | None:
    """Get current price from Binance."""
    try:
        resp = requests.get(BINANCE_PRICE_URL, params={"symbol": symbol}, timeout=10)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        log.warning("Failed to fetch %s price: %s", symbol, e)
        return None


def fetch_volatility(symbol: str = "BTCUSDT", interval: str = "1h", periods: int = 24) -> float | None:
    """Estimate annualized volatility from recent hourly candles."""
    try:
        resp = requests.get(
            BINANCE_KLINES_URL,
            params={"symbol": symbol, "interval": interval, "limit": periods},
            timeout=10,
        )
        resp.raise_for_status()
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < 2:
            return None
        returns = [(closes[i] / closes[i - 1]) - 1 for i in range(1, len(closes))]
        avg = sum(returns) / len(returns)
        variance = sum((r - avg) ** 2 for r in returns) / len(returns)
        hourly_vol = variance ** 0.5
        return hourly_vol
    except Exception as e:
        log.warning("Failed to fetch volatility: %s", e)
        return None


def estimate_probability(
    current_price: float,
    strike: float,
    hours_left: float,
    hourly_vol: float,
    direction: str = "above",
) -> float:
    """Simple probability estimate: how likely is price to be above/below strike?

    Uses a rough normal approximation based on current distance and volatility.
    Not perfect, but good enough to spot 5%+ mispricings.
    """
    if hours_left <= 0:
        # Already expired
        if direction == "above":
            return 1.0 if current_price > strike else 0.0
        else:
            return 1.0 if current_price < strike else 0.0

    # Expected movement range over remaining time
    vol_over_period = hourly_vol * (hours_left ** 0.5)
    if vol_over_period == 0:
        vol_over_period = 0.001

    # Distance from current price to strike as fraction
    distance = (strike - current_price) / current_price

    # Z-score approximation
    z = distance / vol_over_period

    # Quick CDF approximation (good enough for our purposes)
    # P(price > strike) = P(Z > z) = 1 - Phi(z)
    # Using logistic approximation of normal CDF
    import math
    phi = 1.0 / (1.0 + math.exp(-1.7 * z))  # approx normal CDF

    if direction == "above":
        return 1.0 - phi  # P(price > strike)
    else:
        return phi  # P(price < strike)


def parse_strike_from_question(question: str) -> tuple[str, float, str] | None:
    """Extract asset, strike price, and direction from a Polymarket question.

    Examples:
      "Will the price of Bitcoin be above $66,000 on February 13?" -> ("BTC", 66000, "above")
      "Will Bitcoin reach $150,000 in February?" -> ("BTC", 150000, "above")
      "Will Ethereum dip to $1,600 in February?" -> ("ETH", 1600, "below")
    """
    q = question.lower()

    # Determine asset
    if "bitcoin" in q or "btc" in q:
        asset = "BTC"
    elif "ethereum" in q or "eth" in q:
        asset = "ETH"
    elif "solana" in q or "sol" in q:
        asset = "SOL"
    else:
        return None

    # Determine direction
    if "above" in q or "reach" in q or "hit" in q:
        direction = "above"
    elif "below" in q or "dip" in q or "drop" in q:
        direction = "below"
    else:
        return None

    # Extract price — match $XX,XXX or $XX,XXX.XX patterns
    price_match = re.search(r"\$([0-9,]+(?:\.[0-9]+)?)", question)
    if not price_match:
        return None
    strike = float(price_match.group(1).replace(",", ""))

    return (asset, strike, direction)


ASSET_TO_BINANCE = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}


class CryptoOracle(Strategy):
    name = "crypto_oracle"

    def __init__(self, min_edge: float = 0.03) -> None:
        self.min_edge = min_edge
        # Cache prices per asset so we don't hit Binance for every opportunity
        self._price_cache: dict[str, float] = {}
        self._vol_cache: dict[str, float] = {}

    def _get_price(self, asset: str) -> float | None:
        if asset not in self._price_cache:
            symbol = ASSET_TO_BINANCE.get(asset)
            if not symbol:
                return None
            price = fetch_crypto_price(symbol)
            if price:
                self._price_cache[asset] = price
        return self._price_cache.get(asset)

    def _get_vol(self, asset: str) -> float | None:
        if asset not in self._vol_cache:
            symbol = ASSET_TO_BINANCE.get(asset)
            if not symbol:
                return None
            vol = fetch_volatility(symbol)
            if vol:
                self._vol_cache[asset] = vol
        return self._vol_cache.get(asset)

    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        signals: list[Signal] = []
        self._price_cache.clear()
        self._vol_cache.clear()

        for opp in opportunities:
            parsed = parse_strike_from_question(opp.market.question)
            if not parsed:
                continue

            # CRITICAL: Only trade "Yes" tokens — buying "No" on a likely
            # event is the opposite of what we want
            if opp.outcome.lower() != "yes":
                continue

            asset, strike, direction = parsed
            current_price = self._get_price(asset)
            hourly_vol = self._get_vol(asset)

            if current_price is None or hourly_vol is None:
                continue

            # Calculate hours remaining
            try:
                end_str = opp.market.end_date
                if end_str:
                    # Handle various date formats
                    for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                        try:
                            end_dt = datetime.strptime(end_str, fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue
                    else:
                        continue
                    now = datetime.now(timezone.utc)
                    hours_left = (end_dt - now).total_seconds() / 3600
                    if hours_left <= 0:
                        continue
                else:
                    continue
            except Exception:
                continue

            # Our probability estimate
            our_prob = estimate_probability(current_price, strike, hours_left, hourly_vol, direction)

            # Market's price for "Yes" outcome
            market_yes_price = opp.best_ask  # cost to buy Yes

            # The edge: our estimate vs market price
            # If we think Yes is MORE likely than market says → BUY Yes
            edge = our_prob - market_yes_price

            # TIME DECAY PRIORITY: Prefer high-probability trades near expiry
            # These are like deep in-the-money options about to expire
            # Buy at 0.85, settles at 1.00 → guaranteed-ish 15% return
            time_decay_bonus = 0.0
            if our_prob >= 0.90 and hours_left <= 24:
                time_decay_bonus = (our_prob - 0.90) * 2  # boost near-certain short-term
            if our_prob >= 0.95 and hours_left <= 6:
                time_decay_bonus += 0.05  # extra boost for very short expiry

            effective_edge = edge + time_decay_bonus

            if effective_edge >= self.min_edge:
                # Underpriced Yes — buy it
                limit_price = round(min(opp.best_ask, our_prob - 0.01), 4)
                if limit_price <= 0:
                    continue

                opp.score = edge
                signals.append(
                    Signal(
                        opportunity=opp,
                        side=BUY,
                        price=limit_price,
                        size=0,  # sized by risk manager
                        reason=(
                            f"{asset} ${current_price:,.0f} | strike ${strike:,.0f} {direction} | "
                            f"our_prob={our_prob:.1%} vs market={market_yes_price:.1%} | "
                            f"edge={edge:.1%} | {hours_left:.0f}h left"
                        ),
                    )
                )
                log.info(
                    "EDGE FOUND: %s %s $%s | our=%.1f%% market=%.1f%% edge=%.1f%%",
                    asset, direction, f"{strike:,.0f}", our_prob * 100, market_yes_price * 100, edge * 100,
                )

            elif edge <= -self.min_edge:
                # Overpriced Yes — could sell/short or buy No
                # For now just log it
                log.debug(
                    "Overpriced: %s %s $%s | our=%.1f%% market=%.1f%%",
                    asset, direction, f"{strike:,.0f}", our_prob * 100, market_yes_price * 100,
                )

        signals.sort(key=lambda s: s.opportunity.score, reverse=True)
        log.info("CryptoOracle produced %d signals (scanned %d crypto opps)", len(signals), len([o for o in opportunities if parse_strike_from_question(o.market.question)]))
        return signals
