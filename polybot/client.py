"""Thin wrapper around py-clob-client with convenience helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    BookParams,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
# Side constants (not exported in newer py-clob-client versions)
BUY = "BUY"
SELL = "SELL"

from polybot.config import Config

log = logging.getLogger(__name__)


@dataclass
class Market:
    """Simplified representation of a Polymarket event market."""

    condition_id: str
    question: str
    tokens: list[dict[str, Any]]  # [{token_id, outcome}]
    end_date: str = ""
    active: bool = True
    volume: float = 0.0
    liquidity: float = 0.0
    resolved_outcome: str = ""   # winning outcome (for resolved markets)
    resolution_price: float = 0.0  # final settlement price
    raw: dict[str, Any] = field(default_factory=dict)  # full API response


@dataclass
class OrderBookSnapshot:
    """A point-in-time snapshot of an order book."""

    token_id: str
    bids: list[dict[str, Any]] = field(default_factory=list)
    asks: list[dict[str, Any]] = field(default_factory=list)
    midpoint: float = 0.0
    spread: float = 0.0


class PolyClient:
    """High-level Polymarket client used by the bot."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._clob = ClobClient(
            cfg.clob_host,
            key=cfg.private_key,
            chain_id=cfg.chain_id,
            signature_type=cfg.signature_type,
            funder=cfg.funder_address,
        )
        self._clob.set_api_creds(self._clob.create_or_derive_api_creds())
        log.info("CLOB client authenticated")

    # ── Market discovery ──────────────────────────────────────

    def fetch_active_markets(self, limit: int = 100) -> list[Market]:
        """Fetch active markets from the Gamma API."""
        url = f"{self.cfg.gamma_host}/markets"
        params = {"limit": limit, "active": True, "closed": False, "order": "volume24hr", "ascending": False}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        markets: list[Market] = []
        for m in resp.json():
            tokens = []
            # Parse clobTokenIds and outcomes (both can be JSON strings)
            import json as _json
            clob_ids_raw = m.get("clobTokenIds", "[]")
            outcomes_raw = m.get("outcomes", "[]")
            try:
                clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else (clob_ids_raw or [])
            except (ValueError, TypeError):
                clob_ids = []
            try:
                outcomes_list = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
            except (ValueError, TypeError):
                outcomes_list = []
            for i, tid in enumerate(clob_ids):
                outcome = outcomes_list[i] if i < len(outcomes_list) else f"Outcome {i}"
                tokens.append({"token_id": tid, "outcome": outcome})
            # Fallback: old API format with nested tokens
            if not tokens:
                for t in m.get("tokens", []):
                    tokens.append(
                        {"token_id": t.get("token_id", ""), "outcome": t.get("outcome", "")}
                    )
            if not tokens:
                continue
            markets.append(
                Market(
                    condition_id=m.get("conditionId", m.get("condition_id", "")),
                    question=m.get("question", ""),
                    tokens=tokens,
                    end_date=m.get("endDateIso", m.get("end_date_iso", "")),
                    active=m.get("active", True),
                    volume=float(m.get("volumeNum", m.get("volume", 0))),
                    liquidity=float(m.get("liquidityNum", m.get("liquidity", 0))),
                )
            )
        log.info("Fetched %d active markets", len(markets))
        return markets

    def fetch_event_markets(self, keywords: list[str], limit: int = 50) -> list[Market]:
        """Fetch markets from both /events and /markets endpoints.

        Searches /events by title keywords AND /markets by question keywords
        to catch short-window markets that don't appear under event titles.
        """
        markets: list[Market] = []
        seen_ids: set[str] = set()
        import json as _json

        # 1. Search /events by title (finds daily markets + some short-window)
        url = f"{self.cfg.gamma_host}/events"
        params = {"limit": limit, "active": True, "closed": False, "order": "volume24hr", "ascending": False}
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            for event in resp.json():
                title = event.get("title", "").lower()
                if not any(kw.lower() in title for kw in keywords):
                    continue
                for m in event.get("markets", []):
                    mkt = self._parse_market_dict(m, _json)
                    if mkt and mkt.condition_id not in seen_ids:
                        seen_ids.add(mkt.condition_id)
                        markets.append(mkt)
        except Exception as e:
            log.warning("Failed to fetch events: %s", e)

        # 2. Search /markets directly (catches short-window 5-min markets)
        url = f"{self.cfg.gamma_host}/markets"
        params = {"limit": 200, "active": True, "closed": False, "order": "endDate", "ascending": True}
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            for m in resp.json():
                question = m.get("question", "").lower()
                if not any(kw.lower() in question for kw in keywords):
                    continue
                mkt = self._parse_market_dict(m, _json)
                if mkt and mkt.condition_id not in seen_ids:
                    seen_ids.add(mkt.condition_id)
                    markets.append(mkt)
        except Exception as e:
            log.warning("Failed to fetch markets: %s", e)

        log.info("Fetched %d event markets matching %s", len(markets), keywords)
        return markets

    def _parse_market_dict(self, m: dict, _json) -> Market | None:
        """Parse a raw market dict into a Market object."""
        tokens = []
        clob_ids_raw = m.get("clobTokenIds", "[]")
        outcomes_raw = m.get("outcomes", "[]")
        try:
            clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else (clob_ids_raw or [])
        except (ValueError, TypeError):
            clob_ids = []
        try:
            outcomes_list = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
        except (ValueError, TypeError):
            outcomes_list = []
        for i, tid in enumerate(clob_ids):
            outcome = outcomes_list[i] if i < len(outcomes_list) else f"Outcome {i}"
            tokens.append({"token_id": tid, "outcome": outcome})
        if not tokens:
            return None
        # Prefer endDate (full timestamp) over endDateIso (date only)
        return Market(
            condition_id=m.get("conditionId", m.get("condition_id", "")),
            question=m.get("question", ""),
            tokens=tokens,
            end_date=m.get("endDate", m.get("endDateIso", "")),
            active=m.get("active", True),
            volume=float(m.get("volumeNum", m.get("volume", 0) or 0)),
            liquidity=float(m.get("liquidityNum", m.get("liquidity", 0) or 0)),
        )

    # ── Order book ────────────────────────────────────────────

    def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        """Return an order book snapshot for a single token."""
        book = self._clob.get_order_book(token_id)
        bids = book.bids if hasattr(book, "bids") else []
        asks = book.asks if hasattr(book, "asks") else []

        # CLOB returns bids ascending and asks descending — sort properly
        # Best bid = highest bid, Best ask = lowest ask
        sorted_bids = sorted(bids, key=lambda b: float(b.price), reverse=True)
        sorted_asks = sorted(asks, key=lambda a: float(a.price))

        best_bid = float(sorted_bids[0].price) if sorted_bids else 0.0
        best_ask = float(sorted_asks[0].price) if sorted_asks else 1.0
        midpoint = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        return OrderBookSnapshot(
            token_id=token_id,
            bids=[{"price": float(b.price), "size": float(b.size)} for b in sorted_bids],
            asks=[{"price": float(a.price), "size": float(a.size)} for a in sorted_asks],
            midpoint=midpoint,
            spread=spread,
        )

    def get_midpoint(self, token_id: str) -> float:
        """Return the mid-market price for a token."""
        mid = self._clob.get_midpoint(token_id)
        return float(mid)

    def get_price(self, token_id: str, side: str = BUY) -> float:
        """Return the best price for a given side."""
        price = self._clob.get_price(token_id, side)
        return float(price)

    # ── Order execution ───────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = BUY,
    ) -> dict[str, Any]:
        """Create and post a GTC limit order."""
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed = self._clob.create_order(args)
        resp = self._clob.post_order(signed, OrderType.GTC)
        log.info(
            "Limit %s %.2f @ %.4f on %s -> %s",
            side, size, price, token_id[:12], resp,
        )
        return resp

    def place_market_order(
        self,
        token_id: str,
        amount: float,
        side: str = BUY,
    ) -> dict[str, Any]:
        """Create and post a FOK market order."""
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side,
        )
        signed = self._clob.create_market_order(args)
        resp = self._clob.post_order(signed, OrderType.FOK)
        log.info(
            "Market %s $%.2f on %s -> %s",
            side, amount, token_id[:12], resp,
        )
        return resp

    def cancel_order(self, order_id: str) -> Any:
        return self._clob.cancel(order_id)

    def cancel_all_orders(self) -> Any:
        return self._clob.cancel_all()

    def get_open_orders(self) -> list[Any]:
        return self._clob.get_orders(OpenOrderParams())

    def get_trades(self) -> list[Any]:
        return self._clob.get_trades()

    # ── Historical data (for backtesting) ─────────────────────

    def fetch_resolved_markets(
        self,
        keywords: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Market]:
        """Fetch closed/resolved markets from the Gamma API for backtesting."""
        url = f"{self.cfg.gamma_host}/markets"
        params = {
            "limit": limit,
            "offset": offset,
            "closed": True,
            "order": "endDate",
            "ascending": False,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        import json as _json
        markets: list[Market] = []
        for m in resp.json():
            question = m.get("question", "")
            if keywords and not any(kw.lower() in question.lower() for kw in keywords):
                continue
            tokens = []
            clob_ids_raw = m.get("clobTokenIds", "[]")
            outcomes_raw = m.get("outcomes", "[]")
            try:
                clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else (clob_ids_raw or [])
            except (ValueError, TypeError):
                clob_ids = []
            try:
                outcomes_list = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
            except (ValueError, TypeError):
                outcomes_list = []
            for i, tid in enumerate(clob_ids):
                outcome = outcomes_list[i] if i < len(outcomes_list) else f"Outcome {i}"
                tokens.append({"token_id": tid, "outcome": outcome})
            if not tokens:
                continue
            markets.append(
                Market(
                    condition_id=m.get("conditionId", m.get("condition_id", "")),
                    question=m.get("question", ""),
                    tokens=tokens,
                    end_date=m.get("endDateIso", m.get("end_date_iso", "")),
                    active=False,
                    volume=float(m.get("volumeNum", m.get("volume", 0) or 0)),
                    liquidity=float(m.get("liquidityNum", m.get("liquidity", 0) or 0)),
                    resolved_outcome=m.get("outcome", ""),
                    resolution_price=float(m.get("outcomePrices", "0") or 0) if isinstance(m.get("outcomePrices"), (int, float, str)) else 0.0,
                    raw=m,
                )
            )
        log.info("Fetched %d resolved markets", len(markets))
        return markets

    def fetch_resolved_events(
        self,
        keywords: list[str],
        limit: int = 100,
        offset: int = 0,
    ) -> list[Market]:
        """Fetch closed events with their markets for backtesting."""
        url = f"{self.cfg.gamma_host}/events"
        params = {
            "limit": limit,
            "offset": offset,
            "closed": True,
            "order": "endDate",
            "ascending": False,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        import json as _json
        markets: list[Market] = []
        for event in resp.json():
            title = event.get("title", "").lower()
            if not any(kw.lower() in title for kw in keywords):
                continue
            for m in event.get("markets", []):
                tokens = []
                clob_ids_raw = m.get("clobTokenIds", "[]")
                outcomes_raw = m.get("outcomes", "[]")
                try:
                    clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else (clob_ids_raw or [])
                except (ValueError, TypeError):
                    clob_ids = []
                try:
                    outcomes_list = _json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
                except (ValueError, TypeError):
                    outcomes_list = []
                for i, tid in enumerate(clob_ids):
                    outcome = outcomes_list[i] if i < len(outcomes_list) else f"Outcome {i}"
                    tokens.append({"token_id": tid, "outcome": outcome})
                if not tokens:
                    continue
                markets.append(
                    Market(
                        condition_id=m.get("conditionId", m.get("condition_id", "")),
                        question=m.get("question", ""),
                        tokens=tokens,
                        end_date=m.get("endDateIso", m.get("endDate", "")),
                        active=False,
                        volume=float(m.get("volumeNum", m.get("volume", 0) or 0)),
                        liquidity=float(m.get("liquidityNum", m.get("liquidity", 0) or 0)),
                        resolved_outcome=m.get("outcome", ""),
                        resolution_price=0.0,
                        raw=m,
                    )
                )
        log.info("Fetched %d resolved event markets matching %s", len(markets), keywords)
        return markets

    def fetch_market_trades_history(
        self, condition_id: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Fetch trade history for a specific market (CLOB API)."""
        try:
            url = f"{self.cfg.gamma_host}/trades"
            params = {"market": condition_id, "limit": limit}
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning("Failed to fetch trade history for %s: %s", condition_id, e)
            return []

    def fetch_price_history(
        self, token_id: str, fidelity: int = 60
    ) -> list[dict[str, Any]]:
        """Fetch price time-series for a token from Polymarket CLOB.

        fidelity: interval in seconds (60=1min, 300=5min, 3600=1h)
        """
        try:
            url = f"{self.cfg.clob_host}/prices-history"
            params = {"market": token_id, "interval": "max", "fidelity": fidelity}
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json().get("history", [])
        except Exception as e:
            log.warning("Failed to fetch price history for %s: %s", token_id[:12], e)
            return []
