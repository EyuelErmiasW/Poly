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
from py_clob_client.constants import BUY, SELL

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
        params = {"limit": limit, "active": True, "closed": False}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        markets: list[Market] = []
        for m in resp.json():
            tokens = []
            for t in m.get("tokens", []):
                tokens.append(
                    {"token_id": t.get("token_id", ""), "outcome": t.get("outcome", "")}
                )
            if not tokens:
                continue
            markets.append(
                Market(
                    condition_id=m.get("condition_id", ""),
                    question=m.get("question", ""),
                    tokens=tokens,
                    end_date=m.get("end_date_iso", ""),
                    active=m.get("active", True),
                    volume=float(m.get("volume", 0)),
                    liquidity=float(m.get("liquidity", 0)),
                )
            )
        log.info("Fetched %d active markets", len(markets))
        return markets

    # ── Order book ────────────────────────────────────────────

    def get_order_book(self, token_id: str) -> OrderBookSnapshot:
        """Return an order book snapshot for a single token."""
        book = self._clob.get_order_book(token_id)
        bids = book.bids if hasattr(book, "bids") else []
        asks = book.asks if hasattr(book, "asks") else []

        best_bid = float(bids[0].price) if bids else 0.0
        best_ask = float(asks[0].price) if asks else 1.0
        midpoint = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        return OrderBookSnapshot(
            token_id=token_id,
            bids=[{"price": float(b.price), "size": float(b.size)} for b in bids],
            asks=[{"price": float(a.price), "size": float(a.size)} for a in asks],
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
