"""Real-time BTC price feed from Binance."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

# Binance public API — no authentication needed
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Fallback: CoinGecko simple price
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


@dataclass
class BtcSnapshot:
    """A point-in-time BTC price reading."""

    price: float
    timestamp: float  # unix epoch
    volatility_5m: float = 0.0  # annualized vol estimated from recent 5m candles


class BtcFeed:
    """Fetches live BTC/USDT price and estimates short-term volatility."""

    def __init__(self, cache_seconds: float = 2.0) -> None:
        self._cache_seconds = cache_seconds
        self._last_snapshot: BtcSnapshot | None = None
        self._last_fetch: float = 0.0
        self._price_history: list[float] = []

    def get_price(self) -> BtcSnapshot:
        """Return the latest BTC price, using a short cache to avoid hammering the API."""
        now = time.time()
        if self._last_snapshot and (now - self._last_fetch) < self._cache_seconds:
            return self._last_snapshot

        price = self._fetch_binance()
        if price is None:
            price = self._fetch_coingecko()
        if price is None:
            if self._last_snapshot:
                log.warning("All price feeds failed, returning stale price")
                return self._last_snapshot
            raise RuntimeError("Cannot fetch BTC price from any source")

        self._price_history.append(price)
        # Keep last 30 readings for volatility estimation
        if len(self._price_history) > 30:
            self._price_history = self._price_history[-30:]

        vol = self._estimate_volatility()

        snap = BtcSnapshot(price=price, timestamp=now, volatility_5m=vol)
        self._last_snapshot = snap
        self._last_fetch = now
        log.debug("BTC price: $%.2f (5m vol: %.4f)", price, vol)
        return snap

    def _fetch_binance(self) -> float | None:
        try:
            resp = requests.get(
                BINANCE_TICKER_URL,
                params={"symbol": "BTCUSDT"},
                timeout=5,
            )
            resp.raise_for_status()
            return float(resp.json()["price"])
        except Exception:
            log.warning("Binance price fetch failed", exc_info=True)
            return None

    def _fetch_coingecko(self) -> float | None:
        try:
            resp = requests.get(
                COINGECKO_URL,
                params={"ids": "bitcoin", "vs_currencies": "usd"},
                timeout=5,
            )
            resp.raise_for_status()
            return float(resp.json()["bitcoin"]["usd"])
        except Exception:
            log.warning("CoinGecko price fetch failed", exc_info=True)
            return None

    def _estimate_volatility(self) -> float:
        """Estimate 5-minute volatility from cached price readings.

        Returns annualized volatility.  If not enough data, returns a
        reasonable default for BTC (~60% annual ≈ 0.16% per 5 min).
        """
        if len(self._price_history) < 3:
            return 0.60  # default annual vol

        # Log returns between consecutive readings
        returns = []
        for i in range(1, len(self._price_history)):
            prev = self._price_history[i - 1]
            curr = self._price_history[i]
            if prev > 0:
                returns.append(math.log(curr / prev))

        if not returns:
            return 0.60

        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std = math.sqrt(variance) if variance > 0 else 0.001

        # Annualize: assume each reading ≈ one scan interval (~10s),
        # scale up to annual (525600 minutes / year).
        # This is a rough estimate; the exact scaling depends on scan frequency.
        intervals_per_year = 525_600 * 6  # ~6 readings per minute at 10s intervals
        annualized = std * math.sqrt(intervals_per_year)

        # Clamp to reasonable range
        return max(0.20, min(annualized, 2.0))
