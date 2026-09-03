"""Persistence schema.

Every decision the agent makes is written down so we can later answer
"did this strategy actually make money, and why not?".

Money is always stored as integer **cents**; probabilities as floats in ``[0, 1]``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kalshi_agent.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketRow(Base, TimestampMixin):
    __tablename__ = "markets"

    ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    series_ticker: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    subtitle: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiration_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    snapshots: Mapped[list[MarketSnapshot]] = relationship(back_populates="market")


class MarketSnapshot(Base):
    """Point-in-time top-of-book for a market (the collector writes one per poll)."""

    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_snapshots_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(ForeignKey("markets.ticker", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    yes_bid: Mapped[int | None] = mapped_column(Integer)
    yes_ask: Mapped[int | None] = mapped_column(Integer)
    no_bid: Mapped[int | None] = mapped_column(Integer)
    no_ask: Mapped[int | None] = mapped_column(Integer)
    last_price: Mapped[int | None] = mapped_column(Integer)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    open_interest: Mapped[int] = mapped_column(Integer, default=0)
    yes_depth: Mapped[int | None] = mapped_column(Integer)
    no_depth: Mapped[int | None] = mapped_column(Integer)

    market: Mapped[MarketRow] = relationship(back_populates="snapshots")


class SignalRow(Base):
    """Output of the strategy + probability model for one market at one time."""

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ticker: Mapped[str] = mapped_column(String(64), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="")
    side: Mapped[str] = mapped_column(String(3))
    model_probability: Mapped[float] = mapped_column(Float)
    market_probability: Mapped[float] = mapped_column(Float)
    edge: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    limit_price: Mapped[int] = mapped_column(Integer)
    suggested_contracts: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[dict] = mapped_column(JSON, default=dict)


class AgentDecision(Base):
    """What the engine did with a signal and why (risk verdicts included)."""

    __tablename__ = "agent_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ticker: Mapped[str] = mapped_column(String(64), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))
    decision: Mapped[str] = mapped_column(String(16), index=True)  # trade | skip | reject
    reason: Mapped[str] = mapped_column(Text, default="")
    risk_checks: Mapped[list] = mapped_column(JSON, default=list)
    order_id: Mapped[str | None] = mapped_column(String(64))
    trading_mode: Mapped[str] = mapped_column(String(8))


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),)

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64))
    ticker: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(4))
    side: Mapped[str] = mapped_column(String(3))
    type: Mapped[str] = mapped_column(String(8), default="limit")
    price: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer)
    filled_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), index=True)
    trading_mode: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    fills: Mapped[list[FillRow]] = relationship(back_populates="order")


class FillRow(Base):
    __tablename__ = "fills"

    fill_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id", ondelete="CASCADE"))
    ticker: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(4))
    side: Mapped[str] = mapped_column(String(3))
    count: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    is_taker: Mapped[bool] = mapped_column(Boolean, default=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped[OrderRow] = relationship(back_populates="fills")


class PositionRow(Base):
    """Current net position per market (maintained by the portfolio tracker)."""

    __tablename__ = "positions"

    ticker: Mapped[str] = mapped_column(String(64), primary_key=True)
    trading_mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    yes_contracts: Mapped[int] = mapped_column(Integer, default=0)
    no_contracts: Mapped[int] = mapped_column(Integer, default=0)
    avg_yes_cost: Mapped[float] = mapped_column(Float, default=0.0)
    avg_no_cost: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_cents: Mapped[int] = mapped_column(Integer, default=0)
    fees_cents: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PnlSnapshot(Base):
    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    trading_mode: Mapped[str] = mapped_column(String(8), index=True)
    cash_cents: Mapped[int] = mapped_column(Integer)
    exposure_cents: Mapped[int] = mapped_column(Integer)
    realized_pnl_cents: Mapped[int] = mapped_column(Integer)
    unrealized_pnl_cents: Mapped[int] = mapped_column(Integer)
    fees_cents: Mapped[int] = mapped_column(Integer, default=0)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_strategy_name_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    strategy: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class ErrorRow(Base):
    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    component: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
