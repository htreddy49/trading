from datetime import UTC, datetime, timedelta

from kalshi_agent.backtest.engine import BacktestEngine, HistoricalBar
from kalshi_agent.backtest.metrics import compute_metrics, max_drawdown, max_losing_streak
from kalshi_agent.kalshi.models import Side
from kalshi_agent.risk.engine import RiskLimits
from kalshi_agent.strategy.base import MarketContext, Signal, Strategy


class AlwaysYes(Strategy):
    name = "always_yes"
    version = "test"

    def evaluate(self, ctx: MarketContext):
        ask = ctx.yes_ask
        if ask is None or ctx.position != 0:
            return None
        return Signal(
            ctx.market.ticker,
            Side.YES,
            0.9,
            ctx.market_probability or 0.5,
            ask,
            10,
            strategy=self.name,
        )


def bars(result: str):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    out = []
    for i in range(5):
        out.append(
            HistoricalBar(
                ts=t0 + timedelta(hours=i),
                ticker="M",
                yes_bid=40 + i,
                yes_ask=44 + i,
                result=result if i == 4 else None,
            )
        )
    return out


async def test_backtest_wins_when_yes_settles():
    bt = BacktestEngine(
        AlwaysYes(), starting_cash_cents=10_000, limits=RiskLimits(min_liquidity_contracts=0)
    )
    res = await bt.run(bars("yes"))
    assert res.metrics["trades"] == 1
    assert res.metrics["wins"] == 1
    assert res.metrics["net_pnl_cents"] > 0
    assert len(res.trades) == 1 and res.trades[0]["price"] == 44
    assert res.metrics["fees_cents"] > 0


async def test_backtest_loses_when_no_settles():
    bt = BacktestEngine(
        AlwaysYes(), starting_cash_cents=10_000, limits=RiskLimits(min_liquidity_contracts=0)
    )
    res = await bt.run(bars("no"))
    assert res.metrics["losses"] == 1
    assert res.metrics["net_pnl_cents"] == -(10 * 44) - res.metrics["fees_cents"]
    assert res.metrics["max_drawdown"] > 0


def test_metrics_helpers():
    assert max_drawdown([100, 120, 90, 130]) == 0.25
    assert max_losing_streak([1, -1, -1, 2, -1]) == 2
    m = compute_metrics([100, -50], [1000, 1100, 1050], fees_cents=5, starting_cash_cents=1000)
    assert m["win_rate"] == 0.5 and m["profit_factor"] == 2.0 and m["roi"] == 0.05


def test_load_bars_from_db(db):
    from kalshi_agent.db.models import MarketRow, MarketSnapshot
    from kalshi_agent.db.session import session_scope

    with session_scope(db) as s:
        s.add(MarketRow(ticker="M", result="yes"))
        s.add(MarketSnapshot(ticker="M", yes_bid=40, yes_ask=44))
    loaded = BacktestEngine.load_bars(db)
    assert len(loaded) == 1 and loaded[0].result == "yes" and loaded[0].yes_ask == 44
