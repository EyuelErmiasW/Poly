"""Short Scalper strategy — trades ONLY short-duration (5-15 min) crypto markets.

Focused on the "Up or Down" markets on Polymarket that resolve every 5 or 15
minutes.  Uses multi-signal momentum from Binance to predict direction and
places aggressive bets before each window closes.

Key differences from fast_crypto:
  - Filters exclusively for <=15 min windows (ignores longer markets)
  - Uses 1-second and 1-minute candles for ultra-short-term momentum
  - Weights order-book imbalance more heavily (better short-term predictor)
  - VWAP deviation signal for mean-reversion on quieter markets
  - Adaptive confidence threshold: tighter windows → lower threshold needed
  - Continuous automation: scans every 10-15 seconds to catch new windows
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

from polybot.client import BUY, PolyClient, Market
from polybot.market_analyzer import Opportunity
from polybot.strategies.base import Signal, Strategy

log = logging.getLogger(__name__)

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_DEPTH = "https://api.binance.com/api/v3/depth"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
BINANCE_AGG_TRADES = "https://api.binance.com/api/v3/aggTrades"

# Maximum minutes remaining on a market for us to consider it
MAX_WINDOW_MINUTES = 16  # up to 15-min windows
# Minimum minutes remaining (don't bet on something about to close)
MIN_WINDOW_MINUTES = 1.5

# Confidence thresholds (adaptive by time remaining)
BASE_MIN_CONFIDENCE = 0.20


def get_short_momentum(symbol: str = "BTCUSDT") -> dict:
    """Analyze ultra-short-term price action for direction prediction.

    Tuned for 5-15 min horizons using:
      1. EMA crossover on 1m candles (last 30 candles)
      2. Micro momentum (last 3 min price change)
      3. Volume-weighted direction (are buyers or sellers dominant?)
      4. Order book imbalance (heavier weight than fast_crypto)
      5. VWAP deviation (price above/below recent VWAP)
      6. Recent trade flow (aggTrades analysis)
    """
    reasons = []
    score = 0.0

    # 1 & 2 & 3 & 5: Candle-based signals
    try:
        resp = requests.get(BINANCE_KLINES, params={
            "symbol": symbol, "interval": "1m", "limit": 30,
        }, timeout=5)
        candles = resp.json()
        closes = [float(c[4]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        # Volume-weighted typical price for VWAP
        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]

        if len(closes) >= 15:
            # Signal 1: EMA crossover (fast 3 vs slow 10)
            ema3 = _ema(closes, 3)
            ema10 = _ema(closes, 10)
            if ema3 > ema10:
                score += 0.25
                reasons.append("EMA3>EMA10 (bullish)")
            else:
                score -= 0.25
                reasons.append("EMA3<EMA10 (bearish)")

            # Signal 2: Micro momentum — last 3 candles
            micro = closes[-3:]
            micro_change = (micro[-1] - micro[0]) / micro[0] * 100
            if micro_change > 0.02:
                score += 0.20
                reasons.append(f"Micro +{micro_change:.3f}%")
            elif micro_change < -0.02:
                score -= 0.20
                reasons.append(f"Micro {micro_change:.3f}%")

            # Signal 3: Volume-weighted direction
            # Compare buying volume (close > open) vs selling volume
            buy_vol = 0.0
            sell_vol = 0.0
            for c in candles[-10:]:
                open_p, close_p, vol = float(c[1]), float(c[4]), float(c[5])
                if close_p >= open_p:
                    buy_vol += vol
                else:
                    sell_vol += vol
            total_vol = buy_vol + sell_vol
            if total_vol > 0:
                vol_bias = (buy_vol - sell_vol) / total_vol
                if vol_bias > 0.1:
                    score += 0.15
                    reasons.append(f"Buy volume dominant ({vol_bias:.2f})")
                elif vol_bias < -0.1:
                    score -= 0.15
                    reasons.append(f"Sell volume dominant ({vol_bias:.2f})")

            # Signal 5: VWAP deviation
            total_vp = sum(tp * v for tp, v in zip(typical_prices[-15:], volumes[-15:]))
            total_v = sum(volumes[-15:])
            if total_v > 0:
                vwap = total_vp / total_v
                current = closes[-1]
                vwap_dev = (current - vwap) / vwap * 100
                if vwap_dev > 0.01:
                    score += 0.10
                    reasons.append(f"Above VWAP ({vwap_dev:+.3f}%)")
                elif vwap_dev < -0.01:
                    score -= 0.10
                    reasons.append(f"Below VWAP ({vwap_dev:+.3f}%)")

            # Volume spike detection
            avg_vol = sum(volumes[:-5]) / max(len(volumes) - 5, 1)
            recent_vol = sum(volumes[-5:]) / 5
            if avg_vol > 0:
                vol_ratio = recent_vol / avg_vol
                if vol_ratio > 2.0:
                    # Strong volume confirms direction
                    boost = 0.15 if score > 0 else -0.15
                    score += boost
                    reasons.append(f"Vol spike {vol_ratio:.1f}x")

    except Exception as e:
        log.warning("Candle analysis failed for %s: %s", symbol, e)

    # 4: Order book imbalance (heavy weight for short-term)
    try:
        resp = requests.get(BINANCE_DEPTH, params={
            "symbol": symbol, "limit": 50,
        }, timeout=5)
        book = resp.json()
        bid_vol = sum(float(b[1]) for b in book.get("bids", []))
        ask_vol = sum(float(a[1]) for a in book.get("asks", []))
        total = bid_vol + ask_vol
        if total > 0:
            imbalance = (bid_vol - ask_vol) / total
            # Heavier weight for order book on short timeframes
            if imbalance > 0.05:
                score += 0.25 * min(imbalance / 0.3, 1.0)
                reasons.append(f"Book bullish (imb={imbalance:.2f})")
            elif imbalance < -0.05:
                score -= 0.25 * min(abs(imbalance) / 0.3, 1.0)
                reasons.append(f"Book bearish (imb={imbalance:.2f})")
    except Exception as e:
        log.warning("Order book analysis failed for %s: %s", symbol, e)

    # 6: Recent aggTrades flow (last 60 seconds of trades)
    try:
        resp = requests.get(BINANCE_AGG_TRADES, params={
            "symbol": symbol, "limit": 200,
        }, timeout=5)
        trades = resp.json()
        if trades:
            buy_qty = sum(float(t["q"]) for t in trades if not t["m"])  # maker=False → taker buy
            sell_qty = sum(float(t["q"]) for t in trades if t["m"])     # maker=True → taker sell
            total_qty = buy_qty + sell_qty
            if total_qty > 0:
                flow = (buy_qty - sell_qty) / total_qty
                if abs(flow) > 0.05:
                    score += 0.10 * flow
                    side = "buy" if flow > 0 else "sell"
                    reasons.append(f"Trade flow {side} ({flow:.2f})")
    except Exception as e:
        log.warning("AggTrades analysis failed for %s: %s", symbol, e)

    direction = "Up" if score > 0 else "Down"
    confidence = min(abs(score), 1.0)

    return {
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
    }


def _ema(data: list[float], period: int) -> float:
    if len(data) < period:
        return data[-1]
    multiplier = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    return ema


def parse_window_minutes(question: str) -> int | None:
    """Parse the window duration from a market question.

    Examples:
      "Bitcoin ... 5:35PM-5:40PM ET" → 5
      "Bitcoin ... 5:30PM-5:45PM ET" → 15
      "Bitcoin ... 5:00PM-6:00PM ET" → 60
    """
    match = re.search(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
        question, re.IGNORECASE,
    )
    if not match:
        return None

    h1, m1, ap1 = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    h2, m2, ap2 = int(match.group(4)), int(match.group(5)), match.group(6).upper()

    # Convert to 24h
    if ap1 == "PM" and h1 != 12:
        h1 += 12
    elif ap1 == "AM" and h1 == 12:
        h1 = 0
    if ap2 == "PM" and h2 != 12:
        h2 += 12
    elif ap2 == "AM" and h2 == 12:
        h2 = 0

    total1 = h1 * 60 + m1
    total2 = h2 * 60 + m2
    if total2 <= total1:
        total2 += 24 * 60  # crosses midnight

    return total2 - total1


class ShortScalper(Strategy):
    """Trades only short-duration (5-15 min) up/down crypto markets."""

    name = "short_scalper"

    def __init__(self, min_edge: float = 0.05) -> None:
        self.min_edge = min_edge

    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        signals: list[Signal] = []

        # Group by market question to get both Up and Down outcomes
        market_opps: dict[str, dict[str, Opportunity]] = {}
        for opp in opportunities:
            q = opp.market.question
            if q not in market_opps:
                market_opps[q] = {}
            market_opps[q][opp.outcome] = opp

        for question, outcome_map in market_opps.items():
            q_lower = question.lower()

            if "up or down" not in q_lower:
                continue

            up_opp = outcome_map.get("Up")
            down_opp = outcome_map.get("Down")
            if not up_opp or not down_opp:
                continue

            # Parse window duration — ONLY trade <=15 min windows
            window_mins = parse_window_minutes(question)
            if window_mins is None:
                continue  # skip daily markets — no edge
            if window_mins > 15:
                log.debug("Skipping %s — %d min window (too long)", question[:50], window_mins)
                continue

            # Determine asset and Binance symbol
            if "bitcoin" in q_lower:
                symbol, asset = "BTCUSDT", "BTC"
            elif "ethereum" in q_lower:
                symbol, asset = "ETHUSDT", "ETH"
            elif "solana" in q_lower:
                symbol, asset = "SOLUSDT", "SOL"
            elif "xrp" in q_lower:
                symbol, asset = "XRPUSDT", "XRP"
            else:
                continue

            # Check time remaining
            end_dt = _parse_end_date(up_opp.market.end_date)
            if end_dt is None:
                continue

            now = datetime.now(timezone.utc)
            mins_left = (end_dt - now).total_seconds() / 60

            if mins_left < MIN_WINDOW_MINUTES or mins_left > MAX_WINDOW_MINUTES:
                continue

            # Get momentum signal
            signal_data = get_short_momentum(symbol)
            direction = signal_data["direction"]
            confidence = signal_data["confidence"]

            # Adaptive threshold: shorter time left → slightly lower threshold
            # because we want to capture more opportunities on tight windows
            threshold = BASE_MIN_CONFIDENCE
            if mins_left < 5:
                threshold = BASE_MIN_CONFIDENCE * 0.85  # ~17% threshold
            elif mins_left < 3:
                threshold = BASE_MIN_CONFIDENCE * 0.75  # ~15% threshold

            log.info(
                "[ShortScalper] %s %dmin window | %s conf=%.2f | %s | %.1fmin left | threshold=%.2f",
                asset, window_mins, direction, confidence,
                " + ".join(signal_data["reasons"]), mins_left, threshold,
            )

            if confidence < threshold:
                log.info("Skipping %s — conf %.2f < threshold %.2f", asset, confidence, threshold)
                continue

            # Pick the right outcome
            opp = up_opp if direction == "Up" else down_opp

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
                        f"{asset} {direction} ({window_mins}min window) "
                        f"conf={confidence:.0%} | "
                        f"{' + '.join(signal_data['reasons'])} | "
                        f"{mins_left:.1f}min left"
                    ),
                )
            )

        signals.sort(key=lambda s: s.opportunity.score, reverse=True)
        log.info("ShortScalper produced %d signals", len(signals))
        return signals


def _parse_end_date(end_str: str) -> datetime | None:
    """Parse various Polymarket date formats."""
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(end_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
