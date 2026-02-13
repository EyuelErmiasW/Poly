"""Fast Crypto strategy — trades 5-minute Bitcoin and 15-minute ETH up/down markets.

Instead of trying to be faster than HFT bots DURING the window, we predict
direction BEFORE the window opens using multiple Binance signals:
  - Price momentum (EMA crossover on 1m candles)
  - Volume spike detection (unusual buying/selling pressure)
  - Order book imbalance (more bids vs asks = bullish)
  - RSI divergence (overbought/oversold → mean reversion)

Places $1 bets on "Up" or "Down" before each 5-minute window.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta

import requests

from polybot.client import BUY, PolyClient, Market
from polybot.market_analyzer import Opportunity
from polybot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_DEPTH = "https://api.binance.com/api/v3/depth"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"


def get_momentum_signal(symbol: str = "BTCUSDT") -> dict:
    """Analyze recent price action on Binance to predict short-term direction.

    Returns dict with:
      - direction: "Up" or "Down"
      - confidence: 0.0 to 1.0
      - reasons: list of signal descriptions
    """
    reasons = []
    score = 0.0  # positive = bullish, negative = bearish

    # 1. Price momentum — last 15 one-minute candles
    try:
        resp = requests.get(BINANCE_KLINES, params={
            "symbol": symbol, "interval": "1m", "limit": 15
        }, timeout=5)
        candles = resp.json()
        closes = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]

        if len(closes) >= 15:
            # Short EMA (5) vs Long EMA (15)
            ema5 = _ema(closes, 5)
            ema15 = _ema(closes, 15)

            if ema5 > ema15:
                score += 0.3
                reasons.append(f"EMA5 > EMA15 (bullish momentum)")
            else:
                score -= 0.3
                reasons.append(f"EMA5 < EMA15 (bearish momentum)")

            # Price trend — last 5 candles
            recent = closes[-5:]
            trend = (recent[-1] - recent[0]) / recent[0] * 100
            if trend > 0.05:
                score += 0.2
                reasons.append(f"Price up {trend:.2f}% last 5min")
            elif trend < -0.05:
                score -= 0.2
                reasons.append(f"Price down {trend:.2f}% last 5min")

            # 2. Volume analysis — is recent volume higher than average?
            avg_vol = sum(volumes[:-3]) / max(len(volumes) - 3, 1)
            recent_vol = sum(volumes[-3:]) / 3
            if avg_vol > 0:
                vol_ratio = recent_vol / avg_vol
                if vol_ratio > 1.5:
                    # High volume — trend is likely to continue
                    if trend > 0:
                        score += 0.2
                        reasons.append(f"Volume spike {vol_ratio:.1f}x (confirms uptrend)")
                    else:
                        score -= 0.2
                        reasons.append(f"Volume spike {vol_ratio:.1f}x (confirms downtrend)")

            # 3. RSI — mean reversion signal
            rsi = _rsi(closes, 14)
            if rsi is not None:
                if rsi > 70:
                    score -= 0.15
                    reasons.append(f"RSI={rsi:.0f} overbought (bearish)")
                elif rsi < 30:
                    score += 0.15
                    reasons.append(f"RSI={rsi:.0f} oversold (bullish)")

    except Exception as e:
        log.warning("Momentum analysis failed: %s", e)

    # 4. Order book imbalance
    try:
        resp = requests.get(BINANCE_DEPTH, params={
            "symbol": symbol, "limit": 20
        }, timeout=5)
        book = resp.json()
        bid_volume = sum(float(b[1]) for b in book.get("bids", []))
        ask_volume = sum(float(a[1]) for a in book.get("asks", []))
        total = bid_volume + ask_volume
        if total > 0:
            imbalance = (bid_volume - ask_volume) / total
            if imbalance > 0.1:
                score += 0.15
                reasons.append(f"Order book bullish (bid/ask imbalance={imbalance:.2f})")
            elif imbalance < -0.1:
                score -= 0.15
                reasons.append(f"Order book bearish (bid/ask imbalance={imbalance:.2f})")
    except Exception as e:
        log.warning("Order book analysis failed: %s", e)

    # Convert score to direction and confidence
    direction = "Up" if score > 0 else "Down"
    confidence = min(abs(score), 1.0)

    return {
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
    }


def _ema(data: list[float], period: int) -> float:
    """Calculate exponential moving average."""
    if len(data) < period:
        return data[-1]
    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    return ema


def _rsi(closes: list[float], period: int = 14) -> float | None:
    """Calculate RSI."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def find_upcoming_windows(client: PolyClient, minutes_ahead: int = 30) -> list[dict]:
    """Find upcoming 5-min and 15-min up/down markets from Polymarket events."""
    markets = client.fetch_event_markets(
        keywords=["up or down"],
        limit=100,
    )

    now = datetime.now(timezone.utc)
    upcoming = []

    for m in markets:
        q = m.question.lower()

        # Parse which asset
        if "bitcoin" in q:
            asset = "BTC"
            symbol = "BTCUSDT"
        elif "ethereum" in q:
            asset = "ETH"
            symbol = "ETHUSDT"
        elif "solana" in q:
            asset = "SOL"
            symbol = "SOLUSDT"
        elif "xrp" in q:
            asset = "XRP"
            symbol = "XRPUSDT"
        else:
            continue

        # Parse end time
        end_str = m.end_date
        end_dt = None
        for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
            try:
                end_dt = datetime.strptime(end_str, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue

        if end_dt is None:
            continue

        mins_until_end = (end_dt - now).total_seconds() / 60

        # Only trade markets ending in the next N minutes and at least 2 min left
        if 2 < mins_until_end < minutes_ahead:
            # Parse window duration from title
            # "Bitcoin Up or Down - February 13, 5:35PM-5:40PM ET" → 5 min window
            duration_match = re.search(r"(\d+:\d+[AP]M)-(\d+:\d+[AP]M)", m.question)
            window_mins = 5  # default

            upcoming.append({
                "market": m,
                "asset": asset,
                "symbol": symbol,
                "mins_left": mins_until_end,
                "end_dt": end_dt,
                "window_mins": window_mins,
            })

    # Sort by soonest ending
    upcoming.sort(key=lambda x: x["mins_left"])
    return upcoming


# Minimum confidence to place a trade
MIN_CONFIDENCE = 0.25


class FastCrypto(Strategy):
    name = "fast_crypto"

    def __init__(self, min_edge: float = 0.05) -> None:
        self.min_edge = min_edge

    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        """For fast_crypto, opportunities aren't used in the standard way.
        Instead we scan for upcoming windows and generate signals based on
        momentum analysis. This method handles both flows."""
        signals: list[Signal] = []

        # Group opportunities by market question
        market_opps: dict[str, dict[str, Opportunity]] = {}
        for opp in opportunities:
            q = opp.market.question
            if q not in market_opps:
                market_opps[q] = {}
            market_opps[q][opp.outcome] = opp

        for question, outcome_map in market_opps.items():
            q_lower = question.lower()

            # Only process up/down markets
            if "up or down" not in q_lower:
                continue

            # Need both Up and Down outcomes
            up_opp = outcome_map.get("Up")
            down_opp = outcome_map.get("Down")
            if not up_opp or not down_opp:
                continue

            # Determine asset
            if "bitcoin" in q_lower:
                symbol = "BTCUSDT"
                asset = "BTC"
            elif "ethereum" in q_lower:
                symbol = "ETHUSDT"
                asset = "ETH"
            elif "solana" in q_lower:
                symbol = "SOLUSDT"
                asset = "SOL"
            elif "xrp" in q_lower:
                symbol = "XRPUSDT"
                asset = "XRP"
            else:
                continue

            # Check time remaining
            end_str = up_opp.market.end_date
            end_dt = None
            for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                try:
                    end_dt = datetime.strptime(end_str, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if end_dt is None:
                continue

            now = datetime.now(timezone.utc)
            mins_left = (end_dt - now).total_seconds() / 60
            if mins_left < 2 or mins_left > 30:
                continue

            # Get momentum signal from Binance
            signal = get_momentum_signal(symbol)
            direction = signal["direction"]
            confidence = signal["confidence"]

            log.info(
                "%s %s | direction=%s confidence=%.2f | %s | %.0fmin left",
                asset, question[:40], direction, confidence,
                " + ".join(signal["reasons"]), mins_left,
            )

            if confidence < MIN_CONFIDENCE:
                log.info("Skipping %s — confidence %.2f below threshold %.2f",
                         asset, confidence, MIN_CONFIDENCE)
                continue

            # Pick the right outcome to buy
            if direction == "Up":
                opp = up_opp
            else:
                opp = down_opp

            # Price: buy at the current ask (these are ~$0.50 markets)
            entry_price = opp.best_ask
            if entry_price <= 0 or entry_price >= 0.95:
                continue

            opp.score = confidence

            signals.append(
                Signal(
                    opportunity=opp,
                    side=BUY,
                    price=entry_price,
                    size=0,  # sized by risk manager
                    reason=(
                        f"{asset} {direction} (conf={confidence:.0%}) | "
                        f"{' + '.join(signal['reasons'])} | "
                        f"{mins_left:.0f}min left"
                    ),
                )
            )

        signals.sort(key=lambda s: s.opportunity.score, reverse=True)
        log.info("FastCrypto produced %d signals", len(signals))
        return signals
