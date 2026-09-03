"""Strategy interface.

A strategy looks at one market (plus whatever history/features the engine hands it) and
returns either ``None`` (no opinion) or a :class:`Signal` carrying its **probability
estimate** for YES. The strategy does *not* decide position size or whether to trade --
that is the job of the edge detector and the risk engine, so that every strategy is
subject to the same controls.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kalshi_agent.kalshi.models import Market, Orderbook, Side


@dataclass(slots=True)
class MarketContext:
    """Everything a strategy may look at for one market."""

    market: Market
    orderbook: Orderbook | None = None
    history: list[dict[str, Any]] = field(default_factory=list)  # prior snapshots (oldest first)
    now: datetime | None = None
    position: int = 0  # net contracts held: +YES / -NO
    extra: dict[str, Any] = field(default_factory=dict)  # news / external data features

    @property
    def yes_ask(self) -> int | None:
        if self.orderbook and self.orderbook.best_yes_ask is not None:
            return self.orderbook.best_yes_ask
        return self.market.yes_ask

    @property
    def no_ask(self) -> int | None:
        if self.orderbook and self.orderbook.best_no_ask is not None:
            return self.orderbook.best_no_ask
        return self.market.no_ask

    @property
    def market_probability(self) -> float | None:
        """Market-implied P(YES) from the mid price."""
        mid = self.market.yes_mid
        return mid / 100 if mid is not None else None


@dataclass(slots=True)
class Signal:
    ticker: str
    side: Side
    model_probability: float  # P(YES) according to the strategy
    market_probability: float  # P(YES) implied by the market
    limit_price: int  # cents, price of ``side`` we are willing to pay
    suggested_contracts: int
    confidence: float = 0.5
    rationale: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    strategy: str = ""
    strategy_version: str = ""

    @property
    def edge(self) -> float:
        """Expected value per contract in probability points, net of the price we pay.

        For YES: P_model - price/100. For NO: (1 - P_model) - price/100.
        """
        win_prob = self.model_probability if self.side is Side.YES else 1 - self.model_probability
        return win_prob - self.limit_price / 100


class Strategy(ABC):
    name: str = "base"
    version: str = "0.0.0"
    feeds: tuple[str, ...] = ()  # data feeds this strategy needs in ``ctx.extra``

    def __init__(self, **params: Any) -> None:
        self.params = params

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> Signal | None:
        """Return a signal for this market, or None to pass."""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "params": self.params}
