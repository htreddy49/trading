"""Market collector: polls Kalshi and writes markets + top-of-book snapshots to the DB.

Runs as its own process/container so the trading engine never blocks on data ingestion.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import Engine

from kalshi_agent.config import Settings, get_settings
from kalshi_agent.db.models import ErrorRow, MarketRow, MarketSnapshot
from kalshi_agent.db.session import session_scope
from kalshi_agent.kalshi.client import KalshiClient, KalshiError
from kalshi_agent.kalshi.models import Market, Orderbook
from kalshi_agent.logging import get_logger

log = get_logger(__name__)


class MarketCollector:
    def __init__(
        self,
        client: KalshiClient,
        engine: Engine,
        *,
        series_tickers: list[str] | None = None,
        max_markets: int = 500,
        fetch_orderbooks: bool = False,
    ) -> None:
        self.client = client
        self.engine = engine
        self.series_tickers = series_tickers or []
        self.max_markets = max_markets
        self.fetch_orderbooks = fetch_orderbooks

    @classmethod
    def from_settings(cls, client: KalshiClient, engine: Engine, settings: Settings | None = None):
        settings = settings or get_settings()
        return cls(
            client,
            engine,
            series_tickers=settings.collector_series_tickers,
            max_markets=settings.collector_max_markets,
        )

    async def fetch_markets(self) -> list[Market]:
        markets: list[Market] = []
        if self.series_tickers:
            for series in self.series_tickers:
                async for m in self.client.iter_markets(
                    series_ticker=series, max_markets=self.max_markets
                ):
                    markets.append(m)
        else:
            async for m in self.client.iter_markets(max_markets=self.max_markets):
                markets.append(m)
        return markets

    async def collect_once(self) -> int:
        """Fetch open markets and persist one snapshot per market. Returns count."""
        try:
            markets = await self.fetch_markets()
        except KalshiError as exc:
            self._record_error("collector.fetch_markets", str(exc))
            raise

        books: dict[str, Orderbook] = {}
        if self.fetch_orderbooks:
            for m in markets:
                try:
                    books[m.ticker] = await self.client.get_orderbook(m.ticker)
                except KalshiError as exc:  # keep going; orderbooks are best-effort
                    self._record_error("collector.orderbook", str(exc), {"ticker": m.ticker})

        self.persist(markets, books)
        log.info("collector.snapshot", markets=len(markets), orderbooks=len(books))
        return len(markets)

    def persist(self, markets: list[Market], books: dict[str, Orderbook] | None = None) -> None:
        books = books or {}
        now = datetime.now(UTC)
        with session_scope(self.engine) as session:
            for m in markets:
                row = session.get(MarketRow, m.ticker)
                if row is None:
                    row = MarketRow(ticker=m.ticker)
                    session.add(row)
                row.event_ticker = m.event_ticker
                row.series_ticker = m.series_ticker or (
                    m.event_ticker.split("-")[0] if m.event_ticker else None
                )
                row.title = m.title
                row.subtitle = m.subtitle
                row.status = m.status
                row.open_time = m.open_time
                row.close_time = m.close_time
                row.expiration_time = m.expiration_time
                row.result = m.result

                book = books.get(m.ticker)
                snap = MarketSnapshot(
                    ticker=m.ticker,
                    ts=now,
                    yes_bid=m.yes_bid,
                    yes_ask=m.yes_ask,
                    no_bid=m.no_bid,
                    no_ask=m.no_ask,
                    last_price=m.last_price,
                    volume=m.volume,
                    open_interest=m.open_interest,
                    yes_depth=sum(lvl.quantity for lvl in book.yes) if book else None,
                    no_depth=sum(lvl.quantity for lvl in book.no) if book else None,
                )
                session.add(snap)

    def _record_error(self, component: str, message: str, detail: dict | None = None) -> None:
        log.error("collector.error", component=component, message=message)
        with session_scope(self.engine) as session:
            session.add(ErrorRow(component=component, message=message, detail=detail or {}))

    async def run_forever(self, interval_seconds: float) -> None:
        log.info("collector.start", interval=interval_seconds, series=self.series_tickers)
        while True:
            started = asyncio.get_event_loop().time()
            try:
                await self.collect_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                log.exception("collector.cycle_failed", error=str(exc))
            elapsed = asyncio.get_event_loop().time() - started
            await asyncio.sleep(max(0.0, interval_seconds - elapsed))
