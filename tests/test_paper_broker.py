from kalshi_agent.execution.paper import PaperBroker
from kalshi_agent.kalshi.models import Action, OrderRequest, Side
from kalshi_agent.portfolio.tracker import Portfolio
from kalshi_agent.signals.fees import kalshi_fee_cents


async def test_fill_walks_orderbook(orderbook):
    pf = Portfolio(cash_cents=100_000)
    broker = PaperBroker(pf)
    # buy 200 YES at up to 46: 150 @44 (no bid 56) then 50 @46 (no bid 54)
    req = OrderRequest(ticker="T", action=Action.BUY, side=Side.YES, count=200, yes_price=46)
    res = await broker.submit(req, orderbook)
    assert res.status == "filled"
    assert res.fills == [{"price": 44, "count": 150}, {"price": 46, "count": 50}]
    assert res.avg_price == 44  # VWAP 44.5 rounded; accounting uses exact per-level cost
    assert res.fee_cents == kalshi_fee_cents(44, 150) + kalshi_fee_cents(46, 50)
    assert pf.positions["T"].yes_contracts == 200
    assert pf.positions["T"].yes_cost_cents == 150 * 44 + 50 * 46
    assert pf.cash_cents == 100_000 - (150 * 44 + 50 * 46) - res.fee_cents


async def test_partial_fill(orderbook):
    broker = PaperBroker(Portfolio(cash_cents=100_000))
    req = OrderRequest(ticker="T", action=Action.BUY, side=Side.NO, count=500, no_price=60)
    res = await broker.submit(req, orderbook)
    assert res.status == "partially_filled" and res.filled_count == 100


async def test_rests_without_liquidity(orderbook):
    broker = PaperBroker(Portfolio(cash_cents=100_000))
    req = OrderRequest(ticker="T", action=Action.BUY, side=Side.YES, count=10, yes_price=40)
    res = await broker.submit(req, orderbook)
    assert res.status == "resting" and res.filled_count == 0
    assert (await broker.cancel(res.order_id)).status == "canceled"


async def test_no_book_fills_at_limit():
    broker = PaperBroker(Portfolio(cash_cents=100_000), slippage_cents=1)
    req = OrderRequest(ticker="T", action=Action.BUY, side=Side.YES, count=10, yes_price=40)
    res = await broker.submit(req)
    assert res.status == "filled" and res.avg_price == 40


async def test_rejects_when_broke(orderbook):
    broker = PaperBroker(Portfolio(cash_cents=10))
    req = OrderRequest(ticker="T", action=Action.BUY, side=Side.YES, count=10, yes_price=44)
    res = await broker.submit(req, orderbook)
    assert res.status == "rejected"
