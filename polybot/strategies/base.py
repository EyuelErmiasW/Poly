"""Base strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from polybot.market_analyzer import Opportunity


@dataclass
class Signal:
    """A concrete instruction to place a trade."""

    opportunity: Opportunity
    side: str  # "BUY" or "SELL"
    price: float  # limit price (0 = market order)
    size: float  # in shares
    reason: str = ""


class Strategy(ABC):
    """All strategies implement this interface."""

    name: str = "base"

    @abstractmethod
    def evaluate(self, opportunities: list[Opportunity]) -> list[Signal]:
        """Score opportunities and return trade signals."""
        ...
