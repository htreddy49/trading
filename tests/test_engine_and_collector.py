import httpx
import respx
from sqlalchemy import func, select

from kalshi_agent.collector.service import MarketCollector
from kalshi_agent.config import Settings
from kalshi_agent.db.models import AgentDecision, FillRow, MarketSnapshot, OrderRow, SignalRow
from kalshi_agent.db.session import session_scope
from kalshi_agent.engine.loop import TradingEngine
from kalshi_agent.execution.paper import PaperBroker
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.portfolio.tracker import Portfolio
from kalshi_agent.risk.engine import RiskEngine, RiskLimits
from kalshi_agent.signals.edge import EdgeDetector
from kalshi_agent.strategy.registry import get_strategy

BASE = "https://demo-api.kalshi.co/trade-api/v2"
MARKET = {
    "ticker": "M",
    "event_ticker": "EV",
    "title": "m",
    "status": "open",
    "yes_bid": 40,
    "yes_ask": 44,
    "no_bid": 56,
    "no_ask": 60,
    "volume": 10,
}


@respx.mock
async def test_collector_writes_snapshots(db):
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [MARKET], "cursor": ""})
    )
    async with KalshiClient(BASE) as client:
        n = await MarketCollector(client, db).collect_once()
    assert n == 1
    with session_scope(db) as s:
        assert s.scalar(select(func.count()).select_from(MarketSnapshot)) == 1


@respx.mock
async def test_engine_cycle_paper_trades_and_records(db):
    respx.get(f"{BASE}/exchange/status").mock(
        return_value=httpx.Response(200, json={"trading_active": True})
    )
    respx.get(f"{BASE}/markets").mock(
        return_value=httpx.Response(200, json={"markets": [MARKET], "cursor": ""})
    )
    respx.get(f"{BASE}/markets/M/orderbook").mock(
        return_value=httpx.Response(
            200, json={"orderbook": {"yes": [[40, 100]], "no": [[56, 100]]}}
        )
    )
    # seed history so simple_edge thinks fair value is 55 => YES ask 44 is cheap
    with session_scope(db) as s:
        from kalshi_agent.db.models import MarketRow

        s.add(MarketRow(ticker="M"))
        for _ in range(5):
            s.add(MarketSnapshot(ticker="M", yes_bid=54, yes_ask=56))

    settings = Settings(_env_file=None, risk_kill_switch_file="/nonexistent/KS")
    portfolio = Portfolio(cash_cents=50_000)
    client = KalshiClient(BASE)
    engine = TradingEngine(
        client=client,
        db=db,
        strategy=get_strategy("simple_edge", min_discount=3, contracts=5),
        broker=PaperBroker(portfolio),
        risk=RiskEngine(RiskLimits(kill_switch_file=None)),
        edge=EdgeDetector(0.04),
        portfolio=portfolio,
        settings=settings,
    )
    try:
        n = await engine.run_once()
    finally:
        await client.close()
    assert n == 1
    with session_scope(db) as s:
        assert s.scalar(select(func.count()).select_from(SignalRow)) == 1
        decision = s.scalars(select(AgentDecision)).one()
        assert decision.decision == "trade" and decision.trading_mode == "paper"
        order = s.scalars(select(OrderRow)).one()
        assert order.status == "filled" and order.count == 5 and order.price == 44
        assert s.scalar(select(func.count()).select_from(FillRow)) == 1
    assert portfolio.positions["M"].yes_contracts == 5
