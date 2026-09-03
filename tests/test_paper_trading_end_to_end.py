"""Full-chain test: live index feed -> strategy -> risk -> paper broker -> database.

Runs the real TradingEngine against a local websocket serving index ticks in the shape the
exchange actually sends, and a mocked REST surface. Proves the pieces are wired together
before any of it touches a live window.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from websockets.asyncio.server import serve

from kalshi_agent.config import Settings
from kalshi_agent.data.kalshi_index import KalshiIndexFeed
from kalshi_agent.db.models import AgentDecision, OrderRow, SignalRow
from kalshi_agent.db.session import session_scope
from kalshi_agent.engine.loop import TradingEngine
from kalshi_agent.execution.paper import PaperBroker
from kalshi_agent.kalshi.auth import KalshiSigner, generate_test_key
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.portfolio.tracker import Portfolio
from kalshi_agent.risk.engine import RiskEngine, RiskLimits
from kalshi_agent.signals.edge import EdgeDetector
from kalshi_agent.strategy.registry import get_strategy

BASE = "https://api.elections.kalshi.com/trade-api/v2"

SECONDS_PER_YEAR = 365 * 24 * 3600
# The settlement average sits this far below the strike: enough that the model puts the
# losing side around 91c, comfortably inside the band the strategy trades.
GAP = 20.0
STRIKE = 81_554.0
PORT = 8791


async def index_server(websocket):
    """Serve index ticks: value drifting below the strike, average trailing further behind."""
    async for raw in websocket:
        command = json.loads(raw)
        if command.get("cmd") != "subscribe":
            continue
        await websocket.send(
            json.dumps(
                {
                    "type": "subscribed",
                    "id": command["id"],
                    "msg": {"channel": "cfbenchmarks_value_5hz", "sid": 1},
                }
            )
        )
        for i in range(400):  # enough ticks to pass the volatility-estimate threshold
            value = STRIKE - GAP + (2.0 if i % 2 else -2.0)
            await websocket.send(
                json.dumps(
                    {
                        "type": "cfbenchmarks_value_5hz",
                        "sid": 1,
                        "seq": i + 1,
                        "msg": {
                            "index_id": "BRTI",
                            "value_usd": f"{value:.2f}",
                            "data": {
                                "last_60s_average": f"{STRIKE - GAP:.2f}",
                                # the settlement statistic: 15 below the strike
                                "windowed_average_15min": f"{STRIKE - GAP:.2f}",
                            },
                        },
                    }
                )
            )
            await asyncio.sleep(0)


def _market_model(close):
    from kalshi_agent.kalshi.models import Market

    return Market(
        ticker="KXBTC15M-TEST-15",
        status="active",
        floor_strike=STRIKE,
        close_time=close,
        yes_bid=15,
        yes_ask=17,
        no_bid=83,
        no_ask=85,
    )


def _seed_history(feed, annual_vol=0.50, minutes=10):
    """Ten minutes of 5 Hz ticks at a known volatility, ending now."""
    import math
    import random
    import time

    random.seed(11)
    state = feed.state["BRTI"]
    sigma_per_tick = STRIKE * annual_vol / math.sqrt(SECONDS_PER_YEAR) * math.sqrt(0.2)
    now = time.time()
    value = STRIKE - GAP
    state.history.clear()
    for i in range(minutes * 60 * 5):
        ts = now - (minutes * 60) + i * 0.2
        value += random.gauss(0, sigma_per_tick)
        state.history.append((ts, value))
    state.history.append((now, STRIKE - GAP))
    state.value = STRIKE - GAP


@respx.mock
async def test_paper_trade_from_a_real_index_feed(db):
    close = datetime.now(UTC) + timedelta(seconds=30)  # inside the entry window
    market = {
        "ticker": "KXBTC15M-TEST-15",
        "event_ticker": "KXBTC15M-TEST",
        "status": "active",
        "open_time": (close - timedelta(minutes=15)).isoformat(),
        "close_time": close.isoformat(),
        "floor_strike": STRIKE,
        "yes_bid": 8,
        "yes_ask": 12,
        "no_bid": 88,
        "no_ask": 92,
        "volume": 5000,
    }
    respx.get(f"{BASE}/exchange/status").mock(
        return_value=httpx.Response(200, json={"trading_active": True})
    )
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [market], "cursor": ""})
    )
    # NO is offered at 85c; the model says the contract is worth about 90c.
    respx.get(f"{BASE}/markets/KXBTC15M-TEST-15/orderbook").mock(
        return_value=httpx.Response(
            200, json={"orderbook": {"yes": [[15, 500], [14, 900]], "no": [[83, 400], [82, 600]]}}
        )
    )

    signer = KalshiSigner("k", generate_test_key())
    settings = Settings(
        _env_file=None,
        risk_kill_switch_file="/nonexistent/KS",
        collector_series_tickers=["KXBTC15M"],
    )
    portfolio = Portfolio(cash_cents=100_000)
    client = KalshiClient(BASE, signer)
    feed = KalshiIndexFeed(f"ws://127.0.0.1:{PORT}/trade-api/ws/v2", signer)

    async with serve(index_server, "127.0.0.1", PORT):
        # Let the live socket populate the state, then seed a realistic price history so
        # the volatility estimate is sane. A test cannot wait the ten minutes of wall
        # clock the estimator needs, and it has its own tests; what is under test here is
        # the chain from feed through strategy and risk to a recorded paper order.
        await feed.features(_market_model(close))
        await asyncio.sleep(0.3)
        _seed_history(feed)

        engine = TradingEngine(
            client=client,
            db=db,
            strategy=get_strategy("averaging_gap", contracts=5),
            broker=PaperBroker(portfolio),
            risk=RiskEngine(
                RiskLimits(
                    kill_switch_file=None,
                    min_liquidity_contracts=100,
                    max_market_exposure_cents=100_000,
                    max_total_exposure_cents=100_000,
                )
            ),
            edge=EdgeDetector(min_edge=0.01),
            portfolio=portfolio,
            settings=settings,
            feeds=[feed],
        )
        try:
            await engine.run_once()  # first pass warms the volatility estimate
            await asyncio.sleep(0.4)
            await engine.run_once()
        finally:
            await engine.close()

    with session_scope(db) as session:
        from kalshi_agent.db.models import ErrorRow

        errors = [(e.component, e.message) for e in session.scalars(select(ErrorRow))]
        signal = session.scalars(select(SignalRow)).first()
        decision = session.scalars(select(AgentDecision)).first()
        order = session.scalars(select(OrderRow)).first()

    assert not errors, f"the engine swallowed errors: {errors}"

    assert signal is not None, "the strategy produced no signal from a live index feed"
    assert signal.strategy == "averaging_gap" and signal.side == "no"
    # The model must land inside the band the strategy trades, and beat the ask by more
    # than the fee. If either stops being true the trade below is not the one we designed.
    assert 85 <= signal.features["model_price_c"] <= 96
    assert signal.features["edge_c"] > signal.features["fee_c"] + 2
    assert decision is not None and decision.decision == "trade"
    assert order is not None and order.status == "filled"
    assert order.side == "no" and order.trading_mode == "paper"
    assert order.price == 85, "should buy NO at the best available ask"
    assert portfolio.positions["KXBTC15M-TEST-15"].no_contracts == 5
    assert portfolio.cash_cents < 100_000, "paper cash was debited"


@respx.mock
async def test_engine_declines_when_the_window_is_not_close_to_settling(db):
    close = datetime.now(UTC) + timedelta(minutes=8)  # far outside the entry window
    market = {
        "ticker": "KXBTC15M-EARLY-15",
        "status": "active",
        "open_time": (close - timedelta(minutes=15)).isoformat(),
        "close_time": close.isoformat(),
        "floor_strike": STRIKE,
        "yes_bid": 8,
        "yes_ask": 12,
        "no_bid": 88,
        "no_ask": 92,
    }
    respx.get(f"{BASE}/exchange/status").mock(
        return_value=httpx.Response(200, json={"trading_active": True})
    )
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [market], "cursor": ""})
    )
    respx.get(f"{BASE}/markets/KXBTC15M-EARLY-15/orderbook").mock(
        return_value=httpx.Response(
            200, json={"orderbook": {"yes": [[15, 500]], "no": [[83, 400]]}}
        )
    )

    signer = KalshiSigner("k", generate_test_key())
    settings = Settings(_env_file=None, risk_kill_switch_file="/nonexistent/KS")
    portfolio = Portfolio(cash_cents=100_000)
    client = KalshiClient(BASE, signer)
    engine = TradingEngine(
        client=client,
        db=db,
        strategy=get_strategy("averaging_gap"),
        broker=PaperBroker(portfolio),
        risk=RiskEngine(RiskLimits(kill_switch_file=None)),
        edge=EdgeDetector(),
        portfolio=portfolio,
        settings=settings,
        feeds=[],
    )
    try:
        await engine.run_once()
    finally:
        await engine.close()

    with session_scope(db) as session:
        assert session.scalars(select(SignalRow)).first() is None
        assert session.scalars(select(OrderRow)).first() is None
    assert not portfolio.positions, "no position outside the settlement window"


def test_build_feeds_wires_the_index_feed_with_credentials():
    from kalshi_agent.data.base import build_feeds

    signer = KalshiSigner("k", generate_test_key())
    feeds = build_feeds(["kalshi_index"], ws_url="wss://x/trade-api/ws/v2", signer=signer)
    assert len(feeds) == 1 and isinstance(feeds[0], KalshiIndexFeed)
    with pytest.raises(ValueError, match="credentials"):
        build_feeds(["kalshi_index"])
