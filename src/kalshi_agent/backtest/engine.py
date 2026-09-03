"""Event-driven backtester.

Replays historical snapshots (from the ``market_snapshots`` table or any iterable of
:class:`HistoricalBar`) through the *same* strategy -> edge -> risk -> paper-broker
pipeline used in paper trading, then settles markets on their recorded result. Fees and
slippage are applied on every fill.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import Engine, select

from kalshi_agent.backtest.metrics import compute_metrics
from kalshi_agent.db.models import MarketRow, MarketSnapshot
from kalshi_agent.execution.paper import PaperBroker
from kalshi_agent.kalshi.models import Action, Market, OrderRequest, Side
from kalshi_agent.portfolio.tracker import Portfolio
from kalshi_agent.risk.engine import RiskEngine, RiskLimits, RiskState
from kalshi_agent.signals.edge import EdgeDetector
from kalshi_agent.strategy.base import MarketContext, Strategy


@dataclass(slots=True)
class HistoricalBar:
    ts: datetime
    ticker: str
    yes_bid: int | None
    yes_ask: int | None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int = 0
    result: str | None = None  # 'yes' | 'no' once the market has settled
    title: str = ""

    def to_market(self) -> Market:
        yes_bid, yes_ask = self.yes_bid, self.yes_ask
        no_bid = self.no_bid if self.no_bid is not None else (100 - yes_ask if yes_ask else None)
        no_ask = self.no_ask if self.no_ask is not None else (100 - yes_bid if yes_bid else None)
        return Market(
            ticker=self.ticker,
            title=self.title,
            status="open",
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            last_price=self.last_price,
            volume=self.volume,
            result=self.result,
        )


@dataclass(slots=True)
class BacktestResult:
    metrics: dict[str, float | int | None]
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[tuple[datetime, int]] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        *,
        limits: RiskLimits | None = None,
        starting_cash_cents: int = 100_000,
        min_edge: float = 0.04,
        slippage_cents: int = 1,
        history_window: int = 50,
    ) -> None:
        self.strategy = strategy
        limits = limits or RiskLimits(kill_switch_file=None, min_edge=min_edge)
        limits.kill_switch_file = None
        self.risk = RiskEngine(limits)
        self.edge = EdgeDetector(min_edge=min_edge)
        self.portfolio = Portfolio(cash_cents=starting_cash_cents)
        self.broker = PaperBroker(self.portfolio, slippage_cents=slippage_cents)
        self.history_window = history_window

    @staticmethod
    def load_bars(
        engine: Engine,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        tickers: list[str] | None = None,
    ) -> list[HistoricalBar]:
        stmt = (
            select(MarketSnapshot, MarketRow.result, MarketRow.title)
            .join(MarketRow, MarketRow.ticker == MarketSnapshot.ticker)
            .order_by(MarketSnapshot.ts, MarketSnapshot.ticker)
        )
        if start:
            stmt = stmt.where(MarketSnapshot.ts >= start)
        if end:
            stmt = stmt.where(MarketSnapshot.ts <= end)
        if tickers:
            stmt = stmt.where(MarketSnapshot.ticker.in_(tickers))
        from kalshi_agent.db.session import get_session

        with get_session(engine) as session:
            rows = session.execute(stmt).all()
        return [
            HistoricalBar(
                ts=s.ts,
                ticker=s.ticker,
                yes_bid=s.yes_bid,
                yes_ask=s.yes_ask,
                no_bid=s.no_bid,
                no_ask=s.no_ask,
                last_price=s.last_price,
                volume=s.volume,
                result=result,
                title=title or "",
            )
            for s, result, title in rows
        ]

    async def run(self, bars: Iterable[HistoricalBar]) -> BacktestResult:
        history: dict[str, list[dict]] = {}
        last_mark: dict[str, int | None] = {}
        results: dict[str, str] = {}
        equity_curve: list[tuple[datetime, int]] = []
        trades: list[dict] = []
        decisions: list[dict] = []
        recent_orders: list[tuple[str, datetime]] = []
        daily_pnl = 0
        current_day: date | None = None

        for bar in bars:
            if current_day != bar.ts.date():
                current_day = bar.ts.date()
                daily_pnl = 0

            market = bar.to_market()
            last_mark[bar.ticker] = int(market.yes_mid) if market.yes_mid is not None else None
            if bar.result:
                results[bar.ticker] = bar.result

            ctx = MarketContext(
                market=market,
                history=history.get(bar.ticker, []),
                now=bar.ts,
                position=self.portfolio.net_position(bar.ticker),
            )
            signal = self.strategy.evaluate(ctx)
            hist = history.setdefault(bar.ticker, [])
            hist.append(
                {
                    "ts": bar.ts,
                    "mid": market.yes_mid,
                    "yes_bid": bar.yes_bid,
                    "yes_ask": bar.yes_ask,
                }
            )
            del hist[: -self.history_window]

            if signal is None:
                continue

            edge = self.edge.evaluate(signal)
            pos = self.portfolio.positions.get(bar.ticker)
            state = RiskState(
                position_contracts=self.portfolio.net_position(bar.ticker),
                market_exposure_cents=pos.exposure_cents if pos else 0,
                total_exposure_cents=self.portfolio.exposure_cents,
                daily_pnl_cents=daily_pnl,
                spread_cents=market.spread,
                recent_orders=recent_orders[-200:],
            )
            verdict = self.risk.evaluate(signal, edge, state, now=bar.ts)
            decisions.append(
                {
                    "ts": bar.ts,
                    "ticker": bar.ticker,
                    "side": signal.side.value,
                    "edge": edge.net_edge,
                    "approved": verdict.approved,
                    "reason": verdict.reason,
                }
            )
            if not verdict.approved:
                continue

            request = OrderRequest(
                ticker=bar.ticker,
                action=Action.BUY,
                side=signal.side,
                count=verdict.contracts,
                yes_price=signal.limit_price if signal.side is Side.YES else None,
                no_price=signal.limit_price if signal.side is Side.NO else None,
            )
            result = await self.broker.submit(request)
            if result.filled_count:
                recent_orders.append((bar.ticker, bar.ts))
                trades.append(
                    {
                        "ts": bar.ts,
                        "ticker": bar.ticker,
                        "side": signal.side.value,
                        "count": result.filled_count,
                        "price": result.avg_price,
                        "fee_cents": result.fee_cents,
                        "edge": edge.net_edge,
                    }
                )
            equity_curve.append((bar.ts, self.portfolio.equity_cents(last_mark)))

        # Settle everything with a known result; mark the rest at last mid.
        trade_pnls: list[int] = []
        for ticker, pos in list(self.portfolio.positions.items()):
            if pos.yes_contracts == 0 and pos.no_contracts == 0:
                continue
            if ticker in results:
                realized = self.portfolio.settle(ticker, results[ticker])
                trade_pnls.append(realized - pos.fees_cents)
            else:
                trade_pnls.append(pos.unrealized_pnl_cents(last_mark.get(ticker)) - pos.fees_cents)

        final_equity = self.portfolio.equity_cents(last_mark)
        if equity_curve:
            equity_curve.append((equity_curve[-1][0], final_equity))
        curve = [e for _, e in equity_curve] or [self.portfolio.starting_cash_cents]
        metrics = compute_metrics(
            trade_pnls, curve, self.portfolio.fees_cents, self.portfolio.starting_cash_cents
        )
        return BacktestResult(
            metrics=metrics, trades=trades, equity_curve=equity_curve, decisions=decisions
        )
