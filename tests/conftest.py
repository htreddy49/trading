from __future__ import annotations

import os

import pytest
from sqlalchemy import Engine

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RISK_KILL_SWITCH_FILE", "/nonexistent/KILL_SWITCH")

from kalshi_agent.config import clear_settings_cache, get_settings  # noqa: E402
from kalshi_agent.db.base import Base  # noqa: E402
from kalshi_agent.db.session import make_engine  # noqa: E402
from kalshi_agent.kalshi.models import Market, Orderbook, OrderbookLevel  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_settings():
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def db() -> Engine:
    from sqlalchemy.pool import StaticPool

    from kalshi_agent.db import models  # noqa: F401

    engine = make_engine("sqlite://")
    engine.dispose()
    from sqlalchemy import create_engine

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def market() -> Market:
    return Market(
        ticker="TEST-24DEC31-T1",
        event_ticker="TEST-24DEC31",
        title="Test market",
        status="open",
        yes_bid=40,
        yes_ask=44,
        no_bid=56,
        no_ask=60,
        last_price=42,
        volume=1000,
        open_interest=500,
    )


@pytest.fixture
def orderbook(market: Market) -> Orderbook:
    # YES bids at 38/40, NO bids at 54/56  => yes ask 44, no ask 60
    return Orderbook(
        ticker=market.ticker,
        yes=[OrderbookLevel(price=38, quantity=200), OrderbookLevel(price=40, quantity=100)],
        no=[OrderbookLevel(price=54, quantity=300), OrderbookLevel(price=56, quantity=150)],
    )
