"""The recorder.

One process, one WebSocket connection, everything written to disk. It does not trade and
holds no strategy. Its job is to capture, with local timestamps, the two things no public
dataset contains together: the settlement index as it accumulates, and the order book of
the market settling against it.

It also answers an open question about these markets that nobody appears to have measured
publicly. The strike is the index average over the minute ending at the window open, so it
cannot exist beforehand; markets are created about a day early carrying a placeholder.
How long after the open the real number appears determines whether there is an opportunity
there at all, so the recorder polls each window across its open and records exactly when
the strike is stamped.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kalshi_agent.kalshi.client import KalshiClient, KalshiError
from kalshi_agent.kalshi.models import Market
from kalshi_agent.kalshi.ws import (
    CH_INDEX,
    CH_INDEX_5HZ,
    CH_LIFECYCLE,
    CH_ORDERBOOK,
    CH_TICKER,
    CH_TRADE,
    KalshiWebSocket,
    WebSocketClosedError,
    error_code,
    is_error,
)
from kalshi_agent.logging import get_logger
from kalshi_agent.recorder.book import OrderBook
from kalshi_agent.recorder.writer import RecordWriter

log = get_logger(__name__)

MARKET_CHANNELS = [CH_ORDERBOOK, CH_TICKER, CH_TRADE]

# How much of a window's life to hold a subscription for. Starting before the open catches
# the strike being stamped; holding past the close catches settlement.
SUBSCRIBE_BEFORE_OPEN = timedelta(minutes=6)
UNSUBSCRIBE_AFTER_CLOSE = timedelta(minutes=2)


@dataclass(slots=True)
class RecorderStats:
    messages: int = 0
    gaps: int = 0
    reconnects: int = 0
    errors: int = 0
    index_ticks: int = 0
    book_updates: int = 0
    started_at: float = field(default_factory=time.time)
    last_index_ns: int | None = None

    def as_dict(self) -> dict[str, Any]:
        age = None
        if self.last_index_ns is not None:
            age = round((time.time_ns() - self.last_index_ns) / 1e9, 2)
        return {
            "messages": self.messages,
            "index_ticks": self.index_ticks,
            "book_updates": self.book_updates,
            "gaps": self.gaps,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "uptime_s": round(time.time() - self.started_at),
            "index_age_s": age,
        }


class Recorder:
    def __init__(
        self,
        client: KalshiClient,
        ws: KalshiWebSocket,
        writer: RecordWriter,
        *,
        series: list[str] | None = None,
        index_ids: list[str] | None = None,
        refresh_seconds: float = 20.0,
        stats_seconds: float = 60.0,
        watch_strikes: bool = True,
        strike_poll_seconds: float = 0.25,
        strike_poll_window: float = 45.0,
    ) -> None:
        self.client = client
        self.ws = ws
        self.writer = writer
        self.series = series or ["KXBTC15M"]
        self.index_ids = index_ids or ["BRTI"]
        self.refresh_seconds = refresh_seconds
        self.stats_seconds = stats_seconds
        self.watch_strikes = watch_strikes
        self.strike_poll_seconds = strike_poll_seconds
        self.strike_poll_window = strike_poll_window

        self.stats = RecorderStats()
        self.books: dict[str, OrderBook] = {}
        self.subscribed: set[str] = set()
        self._sids: dict[str, int] = {}  # channel -> subscription id
        self._pending: dict[int, str] = {}  # command id -> what it subscribed to
        self._index_channel = CH_INDEX_5HZ
        self._strike_watched: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stop = asyncio.Event()

    # -- market discovery ---------------------------------------------------------------
    async def discover_markets(self) -> list[Market]:
        """Windows whose subscription period overlaps now, across the configured series."""
        now = datetime.now(UTC)
        found: dict[str, Market] = {}
        for series in self.series:
            try:
                markets, _ = await self.client.get_markets(
                    series_ticker=series, status=None, limit=200
                )
            except KalshiError as exc:
                log.warning("recorder.discover_failed", series=series, error=str(exc))
                continue
            for m in markets:
                if m.close_time is None:
                    continue
                close = _aware(m.close_time)
                open_time = _aware(m.open_time) if m.open_time else close - timedelta(minutes=15)
                if open_time - SUBSCRIBE_BEFORE_OPEN <= now <= close + UNSUBSCRIBE_AFTER_CLOSE:
                    found[m.ticker] = m
        return sorted(found.values(), key=lambda m: _aware(m.close_time))  # type: ignore[arg-type]

    async def refresh_subscriptions(self) -> None:
        markets = await self.discover_markets()
        wanted = {m.ticker for m in markets}
        sid = self._sids.get(CH_ORDERBOOK)

        added = wanted - self.subscribed
        removed = self.subscribed - wanted
        if added:
            if sid is None:
                cmd = await self.ws.subscribe(MARKET_CHANNELS, market_tickers=sorted(added))
                self._pending[cmd] = CH_ORDERBOOK
            else:
                await self.ws.add_markets(sid, sorted(added))
            for ticker in added:
                self.books[ticker] = OrderBook(ticker)
            log.info("recorder.markets_added", tickers=sorted(added))
        if removed and sid is not None:
            await self.ws.drop_markets(sid, sorted(removed))
            for ticker in removed:
                self.books.pop(ticker, None)
            log.info("recorder.markets_dropped", tickers=sorted(removed))
        self.subscribed = wanted

        if self.watch_strikes:
            for m in markets:
                if m.ticker not in self._strike_watched and m.open_time is not None:
                    self._strike_watched.add(m.ticker)
                    self._spawn(self._watch_strike(m))

    # -- strike timing -------------------------------------------------------------------
    async def _watch_strike(self, market: Market) -> None:
        """Poll one market across its open and record when the strike is published."""
        open_time = _aware(market.open_time)  # type: ignore[arg-type]
        delay = (open_time - datetime.now(UTC)).total_seconds() - 2.0
        if delay > 0:
            await _sleep_or_stop(self._stop, delay)
            if self._stop.is_set():
                return
        deadline = time.monotonic() + self.strike_poll_window
        polls = 0
        while time.monotonic() < deadline and not self._stop.is_set():
            polls += 1
            try:
                fresh = await self.client.get_market(market.ticker)
            except KalshiError as exc:
                # A just-created window can 404 briefly; that is itself worth recording.
                self.writer.write(
                    "strike_watch",
                    {"ticker": market.ticker, "poll": polls, "error": str(exc)},
                )
                await asyncio.sleep(self.strike_poll_seconds)
                continue
            strike = fresh.floor_strike if fresh.floor_strike is not None else fresh.cap_strike
            self.writer.write(
                "strike_watch",
                {
                    "ticker": market.ticker,
                    "poll": polls,
                    "open_time": open_time.isoformat(),
                    "seconds_after_open": (datetime.now(UTC) - open_time).total_seconds(),
                    "status": fresh.status,
                    "floor_strike": fresh.floor_strike,
                    "cap_strike": fresh.cap_strike,
                },
            )
            if strike is not None:
                log.info(
                    "recorder.strike_seen",
                    ticker=market.ticker,
                    strike=strike,
                    after_open_s=round((datetime.now(UTC) - open_time).total_seconds(), 2),
                    polls=polls,
                )
                return
            await asyncio.sleep(self.strike_poll_seconds)
        log.warning("recorder.strike_not_seen", ticker=market.ticker, polls=polls)

    # -- message handling ----------------------------------------------------------------
    def handle(self, message: dict[str, Any], received_ns: int) -> None:
        msg_type = message.get("type", "")
        self.stats.messages += 1
        self.writer.write(msg_type or "unknown", message, received_ns=received_ns)

        if msg_type == "subscribed":
            body = message.get("msg") or {}
            channel, sid = body.get("channel"), body.get("sid")
            if isinstance(sid, int) and isinstance(channel, str):
                self._sids[channel] = sid
                log.info("recorder.subscribed", channel=channel, sid=sid)
            return

        if is_error(message):
            self.stats.errors += 1
            code = error_code(message)
            cmd_id = message.get("id")
            log.warning("recorder.ws_error", code=code, body=message.get("msg"))
            # The five-per-second index feed is not enabled on every account.
            if self._pending.get(cmd_id) == CH_INDEX_5HZ:  # type: ignore[arg-type]
                self._index_channel = CH_INDEX
                self._spawn(self._subscribe_index())
            return

        sid, seq = message.get("sid"), message.get("seq")
        if isinstance(sid, int) and isinstance(seq, int):
            gap = self.ws.sequences.check(sid, seq)
            if gap is not None:
                self.stats.gaps += 1
                log.warning(
                    "recorder.sequence_gap", sid=gap.sid, expected=gap.expected, got=gap.received
                )
                for open_book in self.books.values():
                    open_book.mark_stale()
                self._spawn(self.ws.request_snapshot(sid))

        body = message.get("msg") or {}
        if msg_type == "orderbook_snapshot":
            ticker = body.get("market_ticker")
            if ticker:
                self.books.setdefault(ticker, OrderBook(ticker)).apply_snapshot(body, seq)
                self.stats.book_updates += 1
        elif msg_type == "orderbook_delta":
            ticker = body.get("market_ticker")
            book = self.books.get(ticker) if ticker else None
            if book is not None:  # a delta before the snapshot has nothing to apply to
                book.apply_delta(body, seq)
                self.stats.book_updates += 1
        elif msg_type.startswith("cfbenchmarks"):
            self.stats.index_ticks += 1
            self.stats.last_index_ns = received_ns

    # -- loops ----------------------------------------------------------------------------
    async def _subscribe_index(self) -> None:
        cmd = await self.ws.subscribe([self._index_channel], index_ids=self.index_ids)
        self._pending[cmd] = self._index_channel
        log.info("recorder.index_subscribe", channel=self._index_channel, indices=self.index_ids)

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh_subscriptions()
            except WebSocketClosedError:
                return
            except Exception as exc:  # noqa: BLE001 - discovery must never kill the capture
                log.exception("recorder.refresh_failed", error=str(exc))
            await _sleep_or_stop(self._stop, self.refresh_seconds)

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            await _sleep_or_stop(self._stop, self.stats_seconds)
            if self._stop.is_set():
                return
            self.writer.flush()
            log.info("recorder.stats", **self.stats.as_dict(), file=str(self.writer.current_path))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def session(self) -> None:
        """One connection's lifetime. Returns when the socket closes."""
        await self._subscribe_index()
        cmd = await self.ws.subscribe([CH_LIFECYCLE])
        self._pending[cmd] = CH_LIFECYCLE
        self._sids.clear()
        self.subscribed.clear()
        await self.refresh_subscriptions()

        self._spawn(self._refresh_loop())
        self._spawn(self._stats_loop())

        async for message in self.ws:
            self.handle(message, time.time_ns())
            if self._stop.is_set():
                return

    async def run(self, *, max_backoff: float = 30.0) -> None:
        """Record until stopped, reconnecting with backoff whenever the socket drops."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self.ws.connect()
                backoff = 1.0
                await self.session()
            except WebSocketClosedError as exc:
                log.warning("recorder.disconnected", error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep recording across anything
                log.exception("recorder.session_failed", error=str(exc))
            finally:
                await self.ws.close()
                self.writer.flush()
            if self._stop.is_set():
                break
            self.stats.reconnects += 1
            log.info("recorder.reconnecting", seconds=backoff)
            await _sleep_or_stop(self._stop, backoff)
            backoff = min(backoff * 2, max_backoff)

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        await self.ws.close()
        self.writer.close()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately if we are shutting down."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
