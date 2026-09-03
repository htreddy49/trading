"""The trading loop.

Each cycle::

    markets (Kalshi)  ->  context (history from DB)  ->  strategy.evaluate
      -> EdgeDetector  ->  RiskEngine  ->  Broker.submit  ->  DB (signals, decisions, orders)

The same loop drives paper and live trading; only the broker differs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select

from kalshi_agent.config import Settings, TradingMode, get_settings
from kalshi_agent.db.models import (
    AgentDecision,
    ErrorRow,
    FillRow,
    MarketSnapshot,
    OrderRow,
    PnlSnapshot,
    SignalRow,
)
from kalshi_agent.db.session import session_scope
from kalshi_agent.execution.base import Broker
from kalshi_agent.execution.live import LiveBroker
from kalshi_agent.execution.paper import PaperBroker
from kalshi_agent.kalshi.client import KalshiClient, KalshiError
from kalshi_agent.kalshi.models import Action, Market, Orderbook, OrderRequest, Side
from kalshi_agent.logging import get_logger
from kalshi_agent.portfolio.tracker import Portfolio
from kalshi_agent.risk.engine import RiskEngine, RiskLimits, RiskState
from kalshi_agent.signals.edge import EdgeDetector
from kalshi_agent.strategy.base import MarketContext, Strategy
from kalshi_agent.strategy.registry import get_strategy

log = get_logger(__name__)


class TradingEngine:
    def __init__(
        self,
        *,
        client: KalshiClient,
        db: Engine,
        strategy: Strategy,
        broker: Broker,
        risk: RiskEngine,
        edge: EdgeDetector,
        portfolio: Portfolio,
        settings: Settings,
        fetch_orderbooks: bool = True,
    ) -> None:
        self.client = client
        self.db = db
        self.strategy = strategy
        self.broker = broker
        self.risk = risk
        self.edge = edge
        self.portfolio = portfolio
        self.settings = settings
        self.fetch_orderbooks = fetch_orderbooks
        self.recent_orders: list[tuple[str, datetime]] = []
        self.daily_pnl_cents = 0
        self._day = datetime.now(UTC).date()

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, db: Engine | None = None
    ) -> TradingEngine:
        from kalshi_agent.db.session import get_engine

        settings = settings or get_settings()
        db = db or get_engine()
        client = KalshiClient.from_settings(settings)
        portfolio = Portfolio(cash_cents=settings.paper_starting_balance_cents)
        broker: Broker
        if settings.trading_mode is TradingMode.LIVE:
            broker = LiveBroker(client, armed=True)
            log.warning("engine.live_mode", kalshi_env=settings.kalshi_env.value)
        else:
            broker = PaperBroker(portfolio, slippage_cents=settings.paper_fill_slippage_cents)
        return cls(
            client=client,
            db=db,
            strategy=get_strategy(settings.strategy_name, **settings.strategy_params),
            broker=broker,
            risk=RiskEngine(RiskLimits.from_settings(settings)),
            edge=EdgeDetector(min_edge=settings.risk_min_edge),
            portfolio=portfolio,
            settings=settings,
        )

    # -- data ---------------------------------------------------------------------
    def load_history(self, ticker: str, limit: int = 50) -> list[dict]:
        stmt = (
            select(MarketSnapshot)
            .where(MarketSnapshot.ticker == ticker)
            .order_by(MarketSnapshot.ts.desc())
            .limit(limit)
        )
        with session_scope(self.db) as session:
            rows = session.scalars(stmt).all()
        return [
            {
                "ts": r.ts,
                "mid": (r.yes_bid + r.yes_ask) / 2
                if r.yes_bid is not None and r.yes_ask is not None
                else None,
                "yes_bid": r.yes_bid,
                "yes_ask": r.yes_ask,
                "volume": r.volume,
            }
            for r in reversed(rows)
        ]

    async def fetch_markets(self) -> list[Market]:
        markets: list[Market] = []
        series: list[str | None] = list(self.settings.collector_series_tickers) or [None]
        for s in series:
            async for m in self.client.iter_markets(
                series_ticker=s, max_markets=self.settings.collector_max_markets
            ):
                if m.is_open:
                    markets.append(m)
        return markets

    # -- one market ---------------------------------------------------------------
    async def process_market(self, market: Market, *, trading_active: bool = True) -> None:
        orderbook: Orderbook | None = None
        if self.fetch_orderbooks:
            try:
                orderbook = await self.client.get_orderbook(market.ticker)
            except KalshiError as exc:
                self.record_error("engine.orderbook", str(exc), {"ticker": market.ticker})

        ctx = MarketContext(
            market=market,
            orderbook=orderbook,
            history=self.load_history(market.ticker),
            now=datetime.now(UTC),
            position=self.portfolio.net_position(market.ticker),
        )
        signal = self.strategy.evaluate(ctx)
        if signal is None:
            return

        edge = self.edge.evaluate(signal)
        pos = self.portfolio.positions.get(market.ticker)
        state = RiskState(
            position_contracts=self.portfolio.net_position(market.ticker),
            market_exposure_cents=pos.exposure_cents if pos else 0,
            total_exposure_cents=self.portfolio.exposure_cents,
            daily_pnl_cents=self.daily_pnl_cents,
            exchange_trading_active=trading_active,
            orderbook=orderbook,
            spread_cents=market.spread,
            recent_orders=self.recent_orders[-500:],
        )
        verdict = self.risk.evaluate(signal, edge, state)

        with session_scope(self.db) as session:
            sig_row = SignalRow(
                ticker=signal.ticker,
                strategy=signal.strategy,
                strategy_version=signal.strategy_version,
                side=signal.side.value,
                model_probability=signal.model_probability,
                market_probability=signal.market_probability,
                edge=edge.net_edge,
                confidence=signal.confidence,
                limit_price=signal.limit_price,
                suggested_contracts=signal.suggested_contracts,
                rationale=signal.rationale,
                features=signal.features,
            )
            session.add(sig_row)
            session.flush()
            signal_id = sig_row.id

        if not verdict.approved:
            self.record_decision(
                market.ticker, signal_id, "reject", verdict.reason, verdict.as_dicts()
            )
            log.info("engine.reject", ticker=market.ticker, reason=verdict.reason)
            return

        request = OrderRequest(
            ticker=market.ticker,
            action=Action.BUY,
            side=signal.side,
            count=verdict.contracts,
            yes_price=signal.limit_price if signal.side is Side.YES else None,
            no_price=signal.limit_price if signal.side is Side.NO else None,
        )
        result = await self.broker.submit(request, orderbook)
        self.recent_orders.append((market.ticker, datetime.now(UTC)))
        self.record_order(request, result, signal.strategy)
        self.record_decision(
            market.ticker,
            signal_id,
            "trade" if result.status != "rejected" else "reject",
            result.message or result.status,
            verdict.as_dicts(),
            result.order_id,
        )
        log.info(
            "engine.order",
            ticker=market.ticker,
            side=signal.side.value,
            price=signal.limit_price,
            count=verdict.contracts,
            status=result.status,
            mode=self.broker.mode,
        )

    # -- persistence helpers ------------------------------------------------------
    def record_decision(self, ticker, signal_id, decision, reason, checks, order_id=None) -> None:
        with session_scope(self.db) as session:
            session.add(
                AgentDecision(
                    ticker=ticker,
                    signal_id=signal_id,
                    decision=decision,
                    reason=reason,
                    risk_checks=checks,
                    order_id=order_id or None,
                    trading_mode=self.broker.mode,
                )
            )

    def record_order(self, request: OrderRequest, result, strategy: str) -> None:
        if not result.order_id:
            return
        with session_scope(self.db) as session:
            session.add(
                OrderRow(
                    order_id=result.order_id,
                    client_order_id=request.client_order_id,
                    ticker=request.ticker,
                    action=request.action.value,
                    side=request.side.value,
                    type=request.type.value,
                    price=request.limit_price or 0,
                    count=request.count,
                    filled_count=result.filled_count,
                    status=result.status,
                    trading_mode=self.broker.mode,
                    strategy=strategy,
                )
            )
            if result.filled_count:
                session.add(
                    FillRow(
                        fill_id=f"{result.order_id}-fill",
                        order_id=result.order_id,
                        ticker=request.ticker,
                        action=request.action.value,
                        side=request.side.value,
                        count=result.filled_count,
                        price=result.avg_price or request.limit_price or 0,
                        fee_cents=result.fee_cents,
                    )
                )

    def record_error(self, component: str, message: str, detail: dict | None = None) -> None:
        log.error("engine.error", component=component, message=message)
        with session_scope(self.db) as session:
            session.add(ErrorRow(component=component, message=message, detail=detail or {}))

    def record_pnl(self, marks: dict[str, int | None]) -> None:
        with session_scope(self.db) as session:
            session.add(
                PnlSnapshot(
                    trading_mode=self.broker.mode,
                    cash_cents=self.portfolio.cash_cents,
                    exposure_cents=self.portfolio.exposure_cents,
                    realized_pnl_cents=self.portfolio.realized_pnl_cents,
                    unrealized_pnl_cents=self.portfolio.unrealized_pnl_cents(marks),
                    fees_cents=self.portfolio.fees_cents,
                )
            )

    # -- cycle --------------------------------------------------------------------
    async def run_once(self) -> int:
        today = datetime.now(UTC).date()
        if today != self._day:
            self._day, self.daily_pnl_cents = today, 0

        trading_active = True
        try:
            status = await self.client.exchange_status()
            trading_active = status.trading_active
        except KalshiError as exc:
            self.record_error("engine.exchange_status", str(exc))

        markets = await self.fetch_markets()
        marks: dict[str, int | None] = {}
        for market in markets:
            marks[market.ticker] = int(market.yes_mid) if market.yes_mid is not None else None
            try:
                await self.process_market(market, trading_active=trading_active)
            except Exception as exc:  # noqa: BLE001 - one bad market must not stop the cycle
                self.record_error("engine.process_market", str(exc), {"ticker": market.ticker})

        realized_before = self.portfolio.realized_pnl_cents
        self.daily_pnl_cents += self.portfolio.realized_pnl_cents - realized_before
        self.record_pnl(marks)
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        self.recent_orders = [(t, ts) for t, ts in self.recent_orders if ts > cutoff]
        log.info(
            "engine.cycle",
            markets=len(markets),
            cash=self.portfolio.cash_cents,
            exposure=self.portfolio.exposure_cents,
            realized=self.portfolio.realized_pnl_cents,
            mode=self.broker.mode,
        )
        return len(markets)

    async def run_forever(self, interval_seconds: float) -> None:
        log.info(
            "engine.start",
            mode=self.broker.mode,
            strategy=self.strategy.describe(),
            interval=interval_seconds,
        )
        while True:
            started = asyncio.get_event_loop().time()
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("engine.cycle_failed", error=str(exc))
                self.record_error("engine.cycle", str(exc))
            elapsed = asyncio.get_event_loop().time() - started
            await asyncio.sleep(max(0.0, interval_seconds - elapsed))
