"""Backtester — replay strategies against resolved Polymarket markets.

Flow:
  1. Fetch resolved crypto markets from Polymarket Gamma API
     - "Up or Down" short-window markets (5-15 min)
     - Strike-price markets ("Bitcoin above $97,000 on Feb 14?")
  2. For each resolved market, reconstruct what the strategy WOULD have
     seen using historical Binance candle data
  3. Run the appropriate signal model and compare to actual outcome
  4. Track P&L

Uses Polymarket for market discovery + outcomes, Binance for price signals.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import requests

from polybot.strategies.crypto_oracle import (
    estimate_probability,
    parse_strike_from_question,
    ASSET_TO_BINANCE,
)

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

# Asset detection for "Up or Down" markets (word boundary matching)
UPDOWN_ASSET_MAP = [
    (re.compile(r"\bbitcoin\b|\bbtc\b", re.I), "BTC", "BTCUSDT"),
    (re.compile(r"\bethereum\b|\beth\b", re.I), "ETH", "ETHUSDT"),
    (re.compile(r"\bsolana\b|\bsol\b", re.I), "SOL", "SOLUSDT"),
    (re.compile(r"\bxrp\b", re.I), "XRP", "XRPUSDT"),
]


@dataclass
class BacktestTrade:
    """Record of a single simulated trade."""
    market_question: str
    asset: str
    market_type: str            # "updown" or "strike"
    predicted_side: str         # "Up"/"Down" or "Yes"/"No"
    actual_outcome: str         # winning outcome
    confidence: float
    entry_price: float
    bet_size: float
    pnl: float
    won: bool
    end_time: str
    spot_at_signal: float
    # Strike-specific
    strike: float = 0.0
    direction: str = ""
    our_probability: float = 0.0
    market_price: float = 0.0
    edge: float = 0.0
    # Up/Down-specific
    window_mins: int = 0
    hours_left_at_signal: float = 0.0
    fee_cost: float = 0.0
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
    total_fees: float = 0.0

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
            f"  Total fees paid:    ${self.total_fees:.2f}",
            "=" * 60,
        ]

        if self.trades:
            lines.append("")
            lines.append("  TRADE LOG (most recent first):")
            lines.append("  " + "-" * 56)
            for t in reversed(self.trades[-50:]):
                status = "WIN " if t.won else "LOSS"
                if t.market_type == "strike":
                    lines.append(
                        f"  [{status}] {t.asset} {t.direction} ${t.strike:,.0f} | "
                        f"our={t.our_probability:.0%} mkt={t.market_price:.0%} "
                        f"edge={t.edge:+.0%} | "
                        f"spot=${t.spot_at_signal:,.0f} | "
                        f"P&L: ${t.pnl:+.2f}"
                    )
                else:
                    lines.append(
                        f"  [{status}] {t.asset} {t.predicted_side} "
                        f"(conf={t.confidence:.0%}) "
                        f"spot=${t.spot_at_signal:,.0f} | "
                        f"{t.window_mins}min | "
                        f"P&L: ${t.pnl:+.2f}"
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

        # Win rate by market type
        type_stats: dict[str, dict] = {}
        for t in self.trades:
            if t.market_type not in type_stats:
                type_stats[t.market_type] = {"wins": 0, "total": 0, "pnl": 0.0}
            type_stats[t.market_type]["total"] += 1
            type_stats[t.market_type]["pnl"] += t.pnl
            if t.won:
                type_stats[t.market_type]["wins"] += 1

        if type_stats:
            lines.append("  BY MARKET TYPE:")
            lines.append("  " + "-" * 56)
            for mtype, stats in sorted(type_stats.items()):
                wr = stats["wins"] / stats["total"] if stats["total"] else 0
                lines.append(
                    f"  {mtype:>8}: {stats['total']:3d} trades, "
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


# ── Market fetching ──────────────────────────────────────────


def fetch_resolved_crypto_markets(
    days_back: int = 7,
    max_markets: int = 500,
) -> list[dict]:
    """Fetch resolved crypto markets from Polymarket Gamma API.

    Finds both "Up or Down" short-window markets and strike-price markets.
    Paginates deep enough to find crypto markets buried behind FDV/political events.
    """
    log.info("Fetching resolved crypto markets (last %d days)...", days_back)

    all_markets: list[dict] = []
    seen_conditions: set[str] = set()

    # ── Search /events endpoint ──
    offset = 0
    batch_size = 100
    empty_batches = 0
    # Paginate deeper — crypto markets are often buried behind FDV/political events
    max_empty_batches = 8

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
            for m in event.get("markets", []):
                question = m.get("question", "")
                parsed = _parse_resolved_crypto_market(m, question, days_back)
                if parsed and parsed["condition_id"] not in seen_conditions:
                    seen_conditions.add(parsed["condition_id"])
                    all_markets.append(parsed)
                    found_in_batch += 1

        offset += batch_size
        if found_in_batch == 0:
            empty_batches += 1
            if empty_batches >= max_empty_batches:
                break
        else:
            empty_batches = 0

        time.sleep(0.3)

    log.info("Found %d markets from /events endpoint", len(all_markets))

    # ── Search /markets endpoint directly ──
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
            parsed = _parse_resolved_crypto_market(m, question, days_back)
            if parsed and parsed["condition_id"] not in seen_conditions:
                seen_conditions.add(parsed["condition_id"])
                all_markets.append(parsed)
                found_in_batch += 1

        offset += batch_size
        if found_in_batch == 0:
            empty_batches += 1
            if empty_batches >= max_empty_batches:
                break
        else:
            empty_batches = 0

        time.sleep(0.3)

    log.info("Found %d total resolved crypto markets", len(all_markets))
    return all_markets


def _parse_resolved_crypto_market(
    m: dict, question: str, days_back: int,
) -> dict | None:
    """Parse a resolved market dict. Handles both Up/Down and strike-price types."""

    # 1. Parse end date — prefer endDate (has time) over endDateIso (date only)
    end_str = m.get("endDate", m.get("endDateIso", m.get("end_date_iso", "")))
    end_dt = _parse_date(end_str)
    if end_dt is None:
        return None

    # 2. Check lookback window
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    if end_dt < cutoff or end_dt > datetime.now(timezone.utc):
        return None

    # 3. Determine winning outcome
    outcomes_raw = m.get("outcomes", "[]")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
    except (ValueError, TypeError):
        outcomes = []

    prices_raw = m.get("outcomePrices", "[]")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else (prices_raw or [])
    except (ValueError, TypeError):
        prices = []

    winning_outcome = None
    if prices and outcomes and len(prices) == len(outcomes):
        for i, p in enumerate(prices):
            try:
                if float(p) > 0.5:
                    winning_outcome = outcomes[i]
                    break
            except (ValueError, TypeError):
                continue

    if winning_outcome is None:
        return None

    condition_id = m.get("conditionId", m.get("condition_id", ""))
    q_lower = question.lower()

    # ── Try "Up or Down" market ──
    if "up or down" in q_lower:
        asset, symbol = None, None
        for pattern, a, s in UPDOWN_ASSET_MAP:
            if pattern.search(question):
                asset, symbol = a, s
                break
        if not asset:
            return None

        window_mins = _parse_window_mins(question)

        return {
            "type": "updown",
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
        }

    # ── Try strike-price market ──
    parsed = parse_strike_from_question(question)
    if parsed:
        asset, strike, direction = parsed
        symbol = ASSET_TO_BINANCE.get(asset)
        if symbol:
            if direction == "above":
                actual_above_strike = (winning_outcome == "Yes")
            else:
                actual_above_strike = (winning_outcome == "No")

            return {
                "type": "strike",
                "condition_id": condition_id,
                "question": question,
                "asset": asset,
                "symbol": symbol,
                "strike": strike,
                "direction": direction,
                "end_date": end_str,
                "end_dt": end_dt,
                "winning_outcome": winning_outcome,
                "actual_above_strike": actual_above_strike,
                "outcomes": outcomes,
                "outcome_prices": prices,
                "volume": float(m.get("volumeNum", m.get("volume", 0) or 0)),
            }

    return None


# ── Binance historical data ─────────────────────────────────


def fetch_binance_candles_at(
    symbol: str,
    target_time: datetime,
    interval: str = "1m",
    count: int = 30,
) -> list[list] | None:
    """Fetch Binance candles ending at a specific historical time."""
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
        log.warning("Failed to fetch candles for %s at %s: %s", symbol, target_time, e)
        return None


# ── Signal replay: Up/Down momentum ─────────────────────────


def replay_momentum_signal(
    symbol: str,
    candles: list[list],
) -> dict:
    """Replay momentum analysis on historical candles for Up/Down markets.

    Uses EMA crossover, micro momentum, volume-weighted direction,
    VWAP deviation, and volume spike signals.
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

    # Signal 4: VWAP deviation
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

    # Signal 5: Volume spike
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
        "spot_price": closes[-1],
    }


# ── Signal replay: Strike-price oracle ──────────────────────


def replay_oracle_signal(
    symbol: str,
    strike: float,
    direction: str,
    hours_left: float,
    target_time: datetime,
) -> dict | None:
    """Replay what crypto_oracle would have seen at a historical moment."""
    # Fetch 1h candles for volatility
    candles_1h = fetch_binance_candles_at(
        symbol=symbol, target_time=target_time, interval="1h", count=24,
    )
    if not candles_1h or len(candles_1h) < 2:
        return None

    # Fetch 1m candles for spot price
    candles_1m = fetch_binance_candles_at(
        symbol=symbol, target_time=target_time, interval="1m", count=5,
    )
    if not candles_1m:
        return None

    spot_price = float(candles_1m[-1][4])

    # Hourly volatility
    closes = [float(k[4]) for k in candles_1h]
    returns = [(closes[i] / closes[i - 1]) - 1 for i in range(1, len(closes))]
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / len(returns)
    hourly_vol = variance ** 0.5

    if hourly_vol == 0:
        hourly_vol = 0.001

    our_prob = estimate_probability(spot_price, strike, hours_left, hourly_vol, direction)

    return {
        "spot_price": spot_price,
        "hourly_vol": hourly_vol,
        "our_prob": our_prob,
    }


def _estimate_historical_market_price(our_prob: float) -> float:
    """Estimate Yes token price — market captures ~80% of the true signal."""
    market_price = 0.50 + (our_prob - 0.50) * 0.80
    return max(0.05, min(0.95, market_price))


# ── Helpers ──────────────────────────────────────────────────


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
    """Parse window duration from market question like '12:30PM-12:45PM ET'."""
    match = re.search(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
        question, re.IGNORECASE,
    )
    if not match:
        return 5

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


# ── Backtester ───────────────────────────────────────────────


class Backtester:
    """Replays strategies against resolved Polymarket crypto markets."""

    def __init__(
        self,
        bet_size: float = 1.0,
        min_confidence: float = 0.20,
        min_edge: float = 0.03,
        max_window_mins: int = 15,
        lookahead_mins: int = 5,
        lookahead_hours: float = 12.0,
        fee_rate: float = 0.0625,   # Polymarket 15-min crypto taker fee rate (~1.56% at p=0.50)
        slippage: float = 0.02,     # 2 cent slippage on thin books
    ) -> None:
        self.bet_size = bet_size
        self.min_confidence = min_confidence   # for Up/Down markets
        self.min_edge = min_edge               # for strike markets
        self.max_window_mins = max_window_mins # for Up/Down markets
        self.lookahead_mins = lookahead_mins   # for Up/Down markets
        self.lookahead_hours = lookahead_hours # for strike markets
        self.fee_rate = fee_rate               # Polymarket fee_rate_bps as decimal
        self.slippage = slippage               # entry price slippage in cents

    def run(
        self,
        days_back: int = 7,
        max_markets: int = 500,
        assets: list[str] | None = None,
    ) -> BacktestResult:
        """Run a full backtest over resolved markets."""
        result = BacktestResult()

        markets = fetch_resolved_crypto_markets(
            days_back=days_back,
            max_markets=max_markets,
        )

        # Filter by asset
        if assets:
            assets_upper = [a.upper() for a in assets]
            markets = [m for m in markets if m["asset"] in assets_upper]

        # Filter Up/Down by window duration
        markets = [
            m for m in markets
            if m["type"] != "updown" or m["window_mins"] <= self.max_window_mins
        ]

        result.total_markets_scanned = len(markets)
        log.info("Backtesting %d resolved markets...", len(markets))

        if not markets:
            log.warning(
                "No resolved crypto markets found! "
                "Try: --days 14, remove --assets filter, or check that "
                "Polymarket has resolved crypto markets recently."
            )
            return result

        for i, mkt in enumerate(markets):
            if mkt["type"] == "updown":
                trade = self._backtest_updown(mkt)
            else:
                trade = self._backtest_strike(mkt)

            if trade is None:
                continue

            result.trades.append(trade)
            result.total_trades += 1
            result.total_wagered += self.bet_size
            result.total_pnl += trade.pnl
            result.total_fees += trade.fee_cost
            if trade.won:
                result.wins += 1
            else:
                result.losses += 1

            # Rate limit Binance API
            if (i + 1) % 10 == 0:
                log.info("Backtested %d/%d markets so far...", i + 1, len(markets))
                time.sleep(0.5)
            else:
                time.sleep(0.15)

        return result

    def _backtest_updown(self, mkt: dict) -> BacktestTrade | None:
        """Backtest a single Up/Down market using momentum signals."""
        signal_time = mkt["end_dt"] - timedelta(minutes=self.lookahead_mins)

        candles = fetch_binance_candles_at(
            symbol=mkt["symbol"],
            target_time=signal_time,
            interval="1m",
            count=30,
        )
        if candles is None:
            return None

        signal = replay_momentum_signal(mkt["symbol"], candles)

        if signal["confidence"] < self.min_confidence:
            return None

        predicted = signal["direction"]
        actual = mkt["winning_outcome"]
        won = predicted == actual

        # Realistic entry price: 50/50 market + slippage (taker crosses spread)
        # On thin books, expect 1-3 cent slippage against you
        entry_price = 0.50 + self.slippage

        # Polymarket taker fee on 15-min crypto markets:
        # fee(p) = p * (1-p) * fee_rate
        # At p≈0.50, fee_rate ~6.25% → fee ≈ 1.56%
        fee_pct = entry_price * (1.0 - entry_price) * self.fee_rate
        fee_cost = self.bet_size * fee_pct

        if won:
            shares = self.bet_size / entry_price
            gross_pnl = shares * (1.0 - entry_price)
            pnl = gross_pnl - fee_cost
        else:
            pnl = -self.bet_size - fee_cost

        return BacktestTrade(
            market_question=mkt["question"],
            asset=mkt["asset"],
            market_type="updown",
            predicted_side=predicted,
            actual_outcome=actual,
            confidence=signal["confidence"],
            entry_price=entry_price,
            bet_size=self.bet_size,
            pnl=pnl,
            won=won,
            end_time=mkt["end_date"],
            spot_at_signal=signal["spot_price"],
            window_mins=mkt["window_mins"],
            fee_cost=fee_cost,
            reasons=signal["reasons"],
        )

    def _backtest_strike(self, mkt: dict) -> BacktestTrade | None:
        """Backtest a single strike-price market using oracle probability."""
        signal_time = mkt["end_dt"] - timedelta(hours=self.lookahead_hours)
        hours_left = self.lookahead_hours

        signal = replay_oracle_signal(
            symbol=mkt["symbol"],
            strike=mkt["strike"],
            direction=mkt["direction"],
            hours_left=hours_left,
            target_time=signal_time,
        )
        if signal is None:
            return None

        our_prob = signal["our_prob"]
        market_yes_price = _estimate_historical_market_price(our_prob)
        edge = our_prob - market_yes_price

        if edge >= self.min_edge:
            side_chosen = "Yes"
            entry_price = market_yes_price + self.slippage
        elif edge <= -self.min_edge:
            side_chosen = "No"
            entry_price = (1.0 - market_yes_price) + self.slippage
        else:
            return None

        # Standard markets have 0% fees, but add slippage cost
        # Strike-price markets are typically standard (not 15-min crypto)
        fee_pct = 0.0  # standard markets = 0% fee
        fee_cost = self.bet_size * fee_pct

        won = (side_chosen == mkt["winning_outcome"])

        if won:
            shares = self.bet_size / entry_price
            gross_pnl = shares * (1.0 - entry_price)
            pnl = gross_pnl - fee_cost
        else:
            pnl = -self.bet_size - fee_cost

        return BacktestTrade(
            market_question=mkt["question"],
            asset=mkt["asset"],
            market_type="strike",
            predicted_side=side_chosen,
            actual_outcome=mkt["winning_outcome"],
            confidence=abs(edge),
            entry_price=entry_price,
            bet_size=self.bet_size,
            pnl=pnl,
            won=won,
            end_time=mkt["end_date"],
            spot_at_signal=signal["spot_price"],
            strike=mkt["strike"],
            direction=mkt["direction"],
            our_probability=our_prob,
            market_price=market_yes_price,
            edge=edge,
            hours_left_at_signal=hours_left,
            fee_cost=fee_cost,
        )
