"""Pydantic models for the subset of the Kalshi v2 API we use.

Prices are integers in **cents** (1..99). Kalshi returns some fields as cents and some as
dollars-strings depending on endpoint version; we normalise to cents everywhere.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def opposite(self) -> Side:
        return Side.NO if self is Side.YES else Side.YES


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class MarketStatus(StrEnum):
    UNOPENED = "unopened"
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    ACTIVE = "active"  # some list endpoints use "active"
    FINALIZED = "finalized"
    DETERMINED = "determined"


class Market(BaseModel):
    model_config = ConfigDict(extra="allow")

    ticker: str
    event_ticker: str | None = None
    series_ticker: str | None = None
    title: str = ""
    subtitle: str = ""
    status: str = "open"
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    liquidity: int | None = None
    open_time: datetime | None = None
    close_time: datetime | None = None
    expiration_time: datetime | None = None
    result: str | None = None

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def spread(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def is_open(self) -> bool:
        return self.status in {"open", "active"}


class OrderbookLevel(BaseModel):
    price: int
    quantity: int


class Orderbook(BaseModel):
    """Resting bids for YES and NO.

    Kalshi reports each side as ``[[price_cents, count], ...]`` sorted ascending. A resting
    YES bid at ``p`` is equivalent to a NO ask at ``100 - p`` and vice versa.
    """

    ticker: str
    yes: list[OrderbookLevel] = Field(default_factory=list)
    no: list[OrderbookLevel] = Field(default_factory=list)

    @classmethod
    def from_api(cls, ticker: str, payload: dict) -> Orderbook:
        book = payload.get("orderbook", payload)
        return cls(
            ticker=ticker,
            yes=[OrderbookLevel(price=p, quantity=q) for p, q in (book.get("yes") or [])],
            no=[OrderbookLevel(price=p, quantity=q) for p, q in (book.get("no") or [])],
        )

    @property
    def best_yes_bid(self) -> OrderbookLevel | None:
        return max(self.yes, key=lambda lvl: lvl.price) if self.yes else None

    @property
    def best_no_bid(self) -> OrderbookLevel | None:
        return max(self.no, key=lambda lvl: lvl.price) if self.no else None

    @property
    def best_yes_ask(self) -> int | None:
        """Implied YES ask = 100 - best NO bid."""
        best = self.best_no_bid
        return 100 - best.price if best else None

    @property
    def best_no_ask(self) -> int | None:
        best = self.best_yes_bid
        return 100 - best.price if best else None

    def depth(self, side: Side, max_price: int) -> int:
        """Contracts available to *buy* ``side`` at or below ``max_price``."""
        resting = self.no if side is Side.YES else self.yes
        return sum(lvl.quantity for lvl in resting if 100 - lvl.price <= max_price)


class OrderRequest(BaseModel):
    ticker: str
    action: Action
    side: Side
    count: int = Field(gt=0)
    type: OrderType = OrderType.LIMIT
    yes_price: int | None = Field(default=None, ge=1, le=99)
    no_price: int | None = Field(default=None, ge=1, le=99)
    client_order_id: str | None = None
    expiration_ts: int | None = None

    @property
    def limit_price(self) -> int | None:
        """Price of the side we are trading, in cents."""
        return self.yes_price if self.side is Side.YES else self.no_price

    def to_api(self) -> dict:
        body: dict = {
            "ticker": self.ticker,
            "action": self.action.value,
            "side": self.side.value,
            "type": self.type.value,
            "count": self.count,
        }
        if self.yes_price is not None:
            body["yes_price"] = self.yes_price
        if self.no_price is not None:
            body["no_price"] = self.no_price
        if self.client_order_id:
            body["client_order_id"] = self.client_order_id
        if self.expiration_ts is not None:
            body["expiration_ts"] = self.expiration_ts
        return body


class Order(BaseModel):
    model_config = ConfigDict(extra="allow")

    order_id: str
    ticker: str
    action: Action
    side: Side
    type: OrderType = OrderType.LIMIT
    status: str
    yes_price: int | None = None
    no_price: int | None = None
    count: int | None = None
    remaining_count: int | None = None
    fill_count: int | None = None
    client_order_id: str | None = None
    created_time: datetime | None = None


class Fill(BaseModel):
    model_config = ConfigDict(extra="allow")

    trade_id: str
    order_id: str
    ticker: str
    action: Action
    side: Side
    count: int
    yes_price: int
    no_price: int
    is_taker: bool = True
    created_time: datetime | None = None


class Position(BaseModel):
    model_config = ConfigDict(extra="allow")

    ticker: str
    position: int = Field(description="Net contracts: positive = long YES, negative = long NO")
    market_exposure: int = 0
    realized_pnl: int = 0
    total_traded: int = 0
    resting_orders_count: int = 0
    fees_paid: int = 0


class Balance(BaseModel):
    model_config = ConfigDict(extra="allow")

    balance: int = Field(description="Available cash in cents")
    portfolio_value: int = 0


class ExchangeStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    exchange_active: bool = True
    trading_active: bool = True
