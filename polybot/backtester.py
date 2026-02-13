"""Backtester — replay strategies against resolved Polymarket markets.

Flow:
  1. Fetch resolved "up or down" markets from Polymarket Gamma API
  2. For each resolved market, reconstruct what the strategy WOULD have seen
     (simulated order book from historical prices + Binance candle replay)
  3. Run the strategy's evaluate() on those simulated opportunities
  4. Compare the strategy's predicted direction to the actual resolution
  5. Track P&L assuming $1 bets at the market price

This gives a realistic picture of how the bot would have performed on
historical short-duration markets before risking real money.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import requests

from polybot.client import Market
from polybot.market_analyzer import Opportunity

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"


@dataclass
class BacktestTrade:
    """Record of a single simulated trade."""
    market_question: str
    asset: str
    predicted_direction: str
    actual_outcome: str
    confidence: float
    entry_price: float
    bet_size: float
    pnl: float
    won: bool
    window_mins: int
    end_time: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class BacktestResult:
    """Aggregate results from a backtest run."""
    trades: list[BacktestTrade] = field(default_factory=list)
    total_markets_scanned: int = 0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_wagered: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades else 0.0

    @property
    def roi(self) -> float:
        return self.total_pnl / self.total_wagered if self.total_wagered else 0.0

    @property
    def avg_pnl_per_trade(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades else 0.0

    def summary(self) -> str:
        lines = [
            "",
            "=" * 60,
            "  BACKTEST RESULTS",
            "=" * 60,
            f"  Markets scanned:    {self.total_markets_scanned}",
            f"  Trades taken:       {self.total_trades}",
            f"  Wins:               {self.wins}",
            f"  Losses:             {self.losses}",
            f"  Win rate:           {self.win_rate:.1%}",
            f"  Total wagered:      ${self.total_wagered:.2f}",
            f"  Total P&L:          ${self.total_pnl:+.2f}",
            f"  ROI:                {self.roi:+.1%}",
            f"  Avg P&L per trade:  ${self.avg_pnl_per_trade:+.2f}",
            "=" * 60,
        ]

        if self.trades:
            lines.append("")
            lines.append("  TRADE LOG (most recent first):")
            lines.append("  " + "-" * 56)
            for t in reversed(self.trades[-50:]):  # show last 50
                status = "WIN " if t.won else "LOSS"
                lines.append(
                    f"  [{status}] {t.asset} {t.predicted_direction} "
                    f"(conf={t.confidence:.0%}) @ ${t.entry_price:.2f} "
                    f"-> {t.actual_outcome} | P&L: ${t.pnl:+.2f} | "
                    f"{t.window_mins}min"
                )
            lines.append("")

        # Win rate by asset
        asset_stats: dict[str, dict] = {}
        for t in self.trades:
            if t.asset not in asset_stats:
                asset_stats[t.asset] = {"wins": 0, "total": 0, "pnl": 0.0}
            asset_stats[t.asset]["total"] += 1
            asset_stats[t.asset]["pnl"] += t.pnl
            if t.won:
                asset_stats[t.asset]["wins"] += 1

        if asset_stats:
            lines.append("  BY ASSET:")
            lines.append("  " + "-" * 56)
            for asset, stats in sorted(asset_stats.items()):
                wr = stats["wins"] / stats["total"] if stats["total"] else 0
                lines.append(
                    f"  {asset:>5}: {stats['total']:3d} trades, "
                    f"win rate {wr:.0%}, P&L ${stats['pnl']:+.2f}"
                )
            lines.append("")

        # Win rate by confidence bucket
        conf_buckets = {"20-40%": [], "40-60%": [], "60-80%": [], "80-100%": []}
        for t in self.trades:
            if t.confidence < 0.40:
                conf_buckets["20-40%"].append(t)
            elif t.confidence < 0.60:
                conf_buckets["40-60%"].append(t)
            elif t.confidence < 0.80:
                conf_buckets["60-80%"].append(t)
            else:
                conf_buckets["80-100%"].append(t)

        lines.append("  BY CONFIDENCE:")
        lines.append("  " + "-" * 56)
        for bucket, trades in conf_buckets.items():
            if trades:
                bw = sum(1 for t in trades if t.won)
                wr = bw / len(trades)
                bp = sum(t.pnl for t in trades)
                lines.append(
                    f"  {bucket:>8}: {len(trades):3d} trades, "
                    f"win rate {wr:.0%}, P&L ${bp:+.2f}"
                )
        lines.append("")

        return "\n".join(lines)


def fetch_resolved_short_markets(
    days_back: int = 7,
    max_markets: int = 500,
) -> list[dict]:
    """Fetch resolved 'up or down' markets from Polymarket Gamma API.

    Searches BOTH the /events and /markets endpoints to maximize coverage.
    Returns list of dicts with market info + resolution outcome.
    """
    log.info("Fetching resolved short markets (last %d days)...", days_back)

    all_markets = []
    seen_conditions = set()  # deduplicate across endpoints

    # ── Strategy 1: Search /events endpoint (markets nested under events) ──
    offset = 0
    batch_size = 100
    empty_batches = 0

    while len(all_markets) < max_markets:
        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={
                    "limit": batch_size,
                    "offset": offset,
                    "closed": True,
                    "order": "endDate",
                    "ascending": False,
                },
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            log.warning("Failed to fetch events at offset %d: %s", offset, e)
            break

        if not events:
            break

        found_in_batch = 0
        for event in events:
            title = event.get("title", "").lower()
            # Check both event title AND individual market questions
            event_matches = "up or down" in title

            for m in event.get("markets", []):
                question = m.get("question", "")
                q_lower = question.lower()

                # Match on event title OR market question
                if not event_matches and "up or down" not in q_lower:
                    continue

                parsed = _parse_resolved_market(m, question, days_back)
                if parsed and parsed["condition_id"] not in seen_conditions:
                    seen_conditions.add(parsed["condition_id"])
                    all_markets.append(parsed)
                    found_in_batch += 1

        offset += batch_size
        if found_in_batch == 0:
            empty_batches += 1
            if empty_batches >= 3:
                break
        else:
            empty_batches = 0

        time.sleep(0.3)

    log.info("Found %d markets from /events endpoint", len(all_markets))

    # ── Strategy 2: Search /markets endpoint directly ──
    offset = 0
    empty_batches = 0

    while len(all_markets) < max_markets:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "limit": batch_size,
                    "offset": offset,
                    "closed": True,
                    "order": "endDate",
                    "ascending": False,
                },
                timeout=15,
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as e:
            log.warning("Failed to fetch markets at offset %d: %s", offset, e)
            break

        if not markets:
            break

        found_in_batch = 0
        for m in markets:
            question = m.get("question", "")
            q_lower = question.lower()

            if "up or down" not in q_lower:
                continue

            parsed = _parse_resolved_market(m, question, days_back)
            if parsed and parsed["condition_id"] not in seen_conditions:
                seen_conditions.add(parsed["condition_id"])
                all_markets.append(parsed)
                found_in_batch += 1

        offset += batch_size
        if found_in_batch == 0:
            empty_batches += 1
            if empty_batches >= 3:
                break
        else:
            empty_batches = 0

        time.sleep(0.3)

    log.info("Found %d total resolved short markets", len(all_markets))
    return all_markets


def _parse_resolved_market(
    m: dict, question: str, days_back: int,
) -> dict | None:
    """Parse a single market dict into our backtest format.

    Returns None if the market doesn't qualify.
    """
    end_str = m.get("endDateIso", m.get("endDate", m.get("end_date_iso", "")))

    # Parse end date
    end_dt = _parse_date(end_str)
    if end_dt is None:
        return None

    # Check if within our lookback window
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    if end_dt < cutoff:
        return None

    # Don't include future markets
    if end_dt > datetime.now(timezone.utc):
        return None

    # Parse outcomes and determine winner
    outcomes_raw = m.get("outcomes", "[]")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
    except (ValueError, TypeError):
        outcomes = []

    # Get outcome prices to determine winner
    prices_raw = m.get("outcomePrices", "[]")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
    except (ValueError, TypeError):
        prices = []

    # The winning outcome has price ~1.0, losing has ~0.0
    winning_outcome = None
    if prices and outcomes and len(prices) == len(outcomes):
        for i, p in enumerate(prices):
            try:
                if float(p) > 0.5:
                    winning_outcome = outcomes[i]
                    break
            except (ValueError, TypeError):
                continue

    # If outcomePrices didn't work, try the 'outcome' field directly
    if winning_outcome is None:
        outcome_field = m.get("outcome", "")
        if outcome_field in ("Up", "Down"):
            winning_outcome = outcome_field

    if winning_outcome is None:
        log.debug("No winning outcome found for: %s", question[:60])
        return None

    # Parse asset
    q_lower = question.lower()
    if "bitcoin" in q_lower or "btc" in q_lower:
        asset, symbol = "BTC", "BTCUSDT"
    elif "ethereum" in q_lower or "eth" in q_lower:
        asset, symbol = "ETH", "ETHUSDT"
    elif "solana" in q_lower or "sol " in q_lower:
        asset, symbol = "SOL", "SOLUSDT"
    elif "xrp" in q_lower:
        asset, symbol = "XRP", "XRPUSDT"
    else:
        return None

    # Parse window duration
    window_mins = _parse_window_mins(question)

    condition_id = m.get("conditionId", m.get("condition_id", ""))

    return {
        "condition_id": condition_id,
        "question": question,
        "asset": asset,
        "symbol": symbol,
        "end_date": end_str,
        "end_dt": end_dt,
        "winning_outcome": winning_outcome,
        "outcomes": outcomes,
        "window_mins": window_mins,
        "volume": float(m.get("volumeNum", m.get("volume", 0) or 0)),
        "raw": m,
    }


def fetch_binance_candles_at(
    symbol: str,
    target_time: datetime,
    interval: str = "1m",
    count: int = 30,
) -> list[list] | None:
    """Fetch Binance candles ending at a specific historical time.

    This is the key to backtesting: we reconstruct what the bot WOULD have
    seen N minutes before the market closed.
    """
    # endTime in Binance API is milliseconds
    end_ms = int(target_time.timestamp() * 1000)

    try:
        resp = requests.get(
            BINANCE_KLINES,
            params={
                "symbol": symbol,
                "interval": interval,
                "endTime": end_ms,
                "limit": count,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning("Failed to fetch historical candles for %s at %s: %s", symbol, target_time, e)
        return None


def replay_momentum_signal(
    symbol: str,
    candles: list[list],
) -> dict:
    """Replay the short_scalper momentum analysis on historical candles.

    Same logic as short_scalper.get_short_momentum() but using pre-fetched
    historical candles instead of live data.
    """
    reasons = []
    score = 0.0

    if not candles or len(candles) < 10:
        return {"direction": "Up", "confidence": 0.0, "score": 0.0, "reasons": ["insufficient data"]}

    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]

    # Signal 1: EMA crossover (fast 3 vs slow 10)
    if len(closes) >= 10:
        ema3 = _ema(closes, 3)
        ema10 = _ema(closes, 10)
        if ema3 > ema10:
            score += 0.25
            reasons.append("EMA3>EMA10 (bullish)")
        else:
            score -= 0.25
            reasons.append("EMA3<EMA10 (bearish)")

    # Signal 2: Micro momentum — last 3 candles
    if len(closes) >= 3:
        micro = closes[-3:]
        micro_change = (micro[-1] - micro[0]) / micro[0] * 100
        if micro_change > 0.02:
            score += 0.20
            reasons.append(f"Micro +{micro_change:.3f}%")
        elif micro_change < -0.02:
            score -= 0.20
            reasons.append(f"Micro {micro_change:.3f}%")

    # Signal 3: Volume-weighted direction
    if len(candles) >= 10:
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
    if len(typical_prices) >= 15 and len(volumes) >= 15:
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

    # Volume spike
    if len(volumes) >= 10:
        avg_vol = sum(volumes[:-5]) / max(len(volumes) - 5, 1)
        recent_vol = sum(volumes[-5:]) / 5
        if avg_vol > 0:
            vol_ratio = recent_vol / avg_vol
            if vol_ratio > 2.0:
                boost = 0.15 if score > 0 else -0.15
                score += boost
                reasons.append(f"Vol spike {vol_ratio:.1f}x")

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


def _parse_date(date_str: str) -> datetime | None:
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_window_mins(question: str) -> int:
    """Parse window duration from market question."""
    match = re.search(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
        question, re.IGNORECASE,
    )
    if not match:
        return 5  # default

    h1, m1, ap1 = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    h2, m2, ap2 = int(match.group(4)), int(match.group(5)), match.group(6).upper()

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
        total2 += 24 * 60
    return total2 - total1


class Backtester:
    """Replays the short_scalper strategy against resolved markets."""

    def __init__(
        self,
        bet_size: float = 1.0,
        min_confidence: float = 0.20,
        max_window_mins: int = 15,
        lookahead_mins: int = 5,
    ) -> None:
        self.bet_size = bet_size
        self.min_confidence = min_confidence
        self.max_window_mins = max_window_mins
        # How many minutes before market close we simulate the signal
        self.lookahead_mins = lookahead_mins

    def run(
        self,
        days_back: int = 7,
        max_markets: int = 500,
        assets: list[str] | None = None,
    ) -> BacktestResult:
        """Run a full backtest over resolved markets.

        Args:
            days_back: How many days of history to test
            max_markets: Maximum markets to fetch
            assets: Filter to specific assets (e.g. ["BTC", "ETH"])
        """
        result = BacktestResult()

        markets = fetch_resolved_short_markets(
            days_back=days_back,
            max_markets=max_markets,
        )

        # Filter by window duration
        markets = [m for m in markets if m["window_mins"] <= self.max_window_mins]

        # Filter by asset
        if assets:
            assets_upper = [a.upper() for a in assets]
            markets = [m for m in markets if m["asset"] in assets_upper]

        result.total_markets_scanned = len(markets)
        log.info("Backtesting %d resolved markets...", len(markets))

        if not markets:
            log.warning(
                "No resolved markets found! Try: --days 14, remove --assets filter, "
                "or check that Polymarket has closed 'up or down' markets recently."
            )
            return result

        for i, mkt in enumerate(markets):
            # Simulate: what would the bot have seen N minutes before close?
            signal_time = mkt["end_dt"] - timedelta(minutes=self.lookahead_mins)

            # Fetch historical Binance candles at that exact point in time
            candles = fetch_binance_candles_at(
                symbol=mkt["symbol"],
                target_time=signal_time,
                interval="1m",
                count=30,
            )

            if candles is None:
                continue

            # Replay the momentum signal
            signal = replay_momentum_signal(mkt["symbol"], candles)

            if signal["confidence"] < self.min_confidence:
                continue

            # The predicted direction
            predicted = signal["direction"]
            actual = mkt["winning_outcome"]

            # Simulate the bet
            # In a real market the entry price would be ~0.50 for 50/50 markets
            # We approximate: if the market is fairly priced, entry ~= 0.50
            # More confident signals might get slightly better prices
            entry_price = 0.50

            won = predicted == actual
            if won:
                # Payout is $1 per share, cost was entry_price per share
                shares = self.bet_size / entry_price
                pnl = shares * (1.0 - entry_price)
            else:
                pnl = -self.bet_size

            trade = BacktestTrade(
                market_question=mkt["question"],
                asset=mkt["asset"],
                predicted_direction=predicted,
                actual_outcome=actual,
                confidence=signal["confidence"],
                entry_price=entry_price,
                bet_size=self.bet_size,
                pnl=pnl,
                won=won,
                window_mins=mkt["window_mins"],
                end_time=mkt["end_date"],
                reasons=signal["reasons"],
            )

            result.trades.append(trade)
            result.total_trades += 1
            result.total_wagered += self.bet_size
            result.total_pnl += pnl
            if won:
                result.wins += 1
            else:
                result.losses += 1

            # Rate limit Binance API
            if (i + 1) % 10 == 0:
                log.info("Backtested %d/%d markets so far...", i + 1, len(markets))
                time.sleep(0.5)
            else:
                time.sleep(0.1)

        return result
