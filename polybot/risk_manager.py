"""Risk management — sizes positions and enforces exposure limits."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from polybot.config import Config
from polybot.strategies.base import Signal

log = logging.getLogger(__name__)


@dataclass
class Position:
    token_id: str
    outcome: str
    side: str
    avg_price: float
    size: float
    cost: float  # total USDC committed


class RiskManager:
    """Enforces per-trade and portfolio-level risk limits."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.positions: dict[str, Position] = {}

    @property
    def total_exposure(self) -> float:
        return sum(p.cost for p in self.positions.values())

    def size_signal(self, signal: Signal) -> Signal | None:
        """Apply position sizing and risk checks.  Returns None if rejected."""
        remaining = self.cfg.max_total_exposure - self.total_exposure
        if remaining <= 0:
            log.warning("Max exposure reached (%.2f), skipping signal", self.total_exposure)
            return None

        # Size based on score (higher score -> larger position, up to max)
        # For small accounts ($1 max trade), always use $1 if signal passed strategy filters
        score = signal.opportunity.score
        raw_size_usd = self.cfg.max_trade_size * max(min(score * 2, 1.0), 0.5)
        size_usd = min(raw_size_usd, remaining, self.cfg.max_trade_size)
        # Ensure we hit the minimum viable trade size
        size_usd = max(size_usd, min(1.0, remaining, self.cfg.max_trade_size))

        if size_usd < 1.0:
            log.debug("Computed size too small ($%.2f), skipping", size_usd)
            return None

        # Convert USD amount to share count at the limit price
        if signal.price <= 0 or signal.price >= 1:
            return None

        shares = round(size_usd / signal.price, 2)
        signal.size = shares

        log.info(
            "Sized: %s %.2f shares @ %.4f ($%.2f) — %s",
            signal.side, shares, signal.price, size_usd, signal.reason,
        )
        return signal

    def record_fill(self, signal: Signal) -> None:
        """Track a filled order as a position."""
        cost = signal.size * signal.price
        key = signal.opportunity.token_id
        if key in self.positions:
            pos = self.positions[key]
            pos.size += signal.size
            pos.cost += cost
            pos.avg_price = pos.cost / pos.size if pos.size else 0
        else:
            self.positions[key] = Position(
                token_id=signal.opportunity.token_id,
                outcome=signal.opportunity.outcome,
                side=signal.side,
                avg_price=signal.price,
                size=signal.size,
                cost=cost,
            )
        log.info(
            "Position recorded: %s %s (total exposure: $%.2f)",
            signal.opportunity.outcome, signal.side, self.total_exposure,
        )
