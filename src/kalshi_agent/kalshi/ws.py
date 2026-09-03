"""Kalshi WebSocket client.

One connection multiplexes every channel we need. The exchange authenticates the socket
itself at the HTTP upgrade, even for public market data, so a signer is always required.

Responsibilities kept here: connecting, authenticating, issuing subscribe and
update-subscription commands, and tracking the per-subscription sequence numbers that
tell us whether we have missed a message. Deciding *what* to subscribe to belongs to the
recorder, and maintaining an order book belongs to :mod:`kalshi_agent.recorder.book`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from kalshi_agent.kalshi.auth import KalshiSigner
from kalshi_agent.logging import get_logger

log = get_logger(__name__)

WS_PATH = "/trade-api/ws/v2"

# Channels carrying market data. ``cfbenchmarks_value`` is the settlement source: it
# publishes the index roughly once a second plus, in the final minute before each quarter
# hour, the windowed average the market actually settles on. The 5hz variant is newer and
# not enabled on every account, so callers should be prepared to fall back.
CH_ORDERBOOK = "orderbook_delta"
CH_TICKER = "ticker"
CH_TRADE = "trade"
CH_LIFECYCLE = "market_lifecycle_v2"
CH_INDEX = "cfbenchmarks_value"
CH_INDEX_5HZ = "cfbenchmarks_value_5hz"

# Errors that invalidate a subscription; the caller must resubscribe rather than retry.
TERMINAL_ERROR_CODES = {10, 17, 25}


class WebSocketClosedError(Exception):
    """Raised when the connection drops; the caller decides whether to reconnect."""


@dataclass(slots=True)
class SequenceGap:
    """A missed message on one subscription. The local book is no longer trustworthy."""

    sid: int
    expected: int
    received: int


@dataclass(slots=True)
class SequenceTracker:
    """Per-subscription monotonic sequence checking.

    Kalshi numbers messages per ``sid``. A skipped number means we lost state and the
    order book cannot be repaired from later deltas, so we must re-snapshot. Duplicates
    (a number we have already seen) are ignored rather than treated as an error.
    """

    last: dict[int, int] = field(default_factory=dict)

    def check(self, sid: int, seq: int) -> SequenceGap | None:
        previous = self.last.get(sid)
        if previous is not None:
            if seq <= previous:
                return None  # duplicate or replay; harmless
            if seq != previous + 1:
                self.last[sid] = seq
                return SequenceGap(sid=sid, expected=previous + 1, received=seq)
        self.last[sid] = seq
        return None

    def reset(self, sid: int | None = None) -> None:
        if sid is None:
            self.last.clear()
        else:
            self.last.pop(sid, None)


class KalshiWebSocket:
    """A single authenticated connection.

    Usage::

        async with KalshiWebSocket(url, signer) as ws:
            await ws.subscribe([CH_ORDERBOOK], market_tickers=["KXBTC15M-..."])
            async for message in ws:
                ...
    """

    def __init__(
        self,
        url: str,
        signer: KalshiSigner,
        *,
        open_timeout: float = 15.0,
        ping_interval: float | None = 20.0,
        connect: Any = None,
    ) -> None:
        self.url = url
        self.signer = signer
        self.open_timeout = open_timeout
        self.ping_interval = ping_interval
        self._connect = connect or websockets.connect
        self._ws: ClientConnection | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.sequences = SequenceTracker()

    # -- lifecycle -------------------------------------------------------------------
    async def connect(self) -> None:
        headers = self.signer.headers("GET", WS_PATH)
        self._ws = await self._connect(
            self.url,
            additional_headers=headers,
            open_timeout=self.open_timeout,
            ping_interval=self.ping_interval,
        )
        self.sequences.reset()
        log.info("ws.connected", url=self.url)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> KalshiWebSocket:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- commands --------------------------------------------------------------------
    async def _send(self, cmd: str, params: dict[str, Any]) -> int:
        if self._ws is None:
            raise WebSocketClosedError("not connected")
        message_id = self._next_id
        self._next_id += 1
        await self._ws.send(json.dumps({"id": message_id, "cmd": cmd, "params": params}))
        return message_id

    async def subscribe(
        self,
        channels: list[str],
        *,
        market_tickers: list[str] | None = None,
        index_ids: list[str] | None = None,
    ) -> int:
        """Send a subscribe command. Returns the command id; the ack arrives as a message.

        ``market_tickers`` and ``index_ids`` are mutually exclusive in practice: the index
        channels take index ids and reject market tickers, and vice versa.
        """
        params: dict[str, Any] = {"channels": channels}
        if market_tickers:
            params["market_tickers"] = market_tickers
        if index_ids:
            params["index_ids"] = index_ids
        log.debug("ws.subscribe", channels=channels, markets=market_tickers, indices=index_ids)
        return await self._send("subscribe", params)

    async def unsubscribe(self, sids: list[int]) -> int:
        for sid in sids:
            self.sequences.reset(sid)
        return await self._send("unsubscribe", {"sids": sids})

    async def add_markets(self, sid: int, market_tickers: list[str]) -> int:
        return await self._send(
            "update_subscription",
            {"sid": sid, "action": "add_markets", "market_tickers": market_tickers},
        )

    async def drop_markets(self, sid: int, market_tickers: list[str]) -> int:
        return await self._send(
            "update_subscription",
            {"sid": sid, "action": "delete_markets", "market_tickers": market_tickers},
        )

    async def request_snapshot(self, sid: int, market_tickers: list[str] | None = None) -> int:
        """Ask for a fresh order book snapshot without tearing down the subscription.

        This is the correct repair for a sequence gap: reconnecting would drop every other
        subscription on the connection too.
        """
        params: dict[str, Any] = {"sid": sid, "action": "get_snapshot"}
        if market_tickers:
            params["market_tickers"] = market_tickers
        self.sequences.reset(sid)
        return await self._send("update_subscription", params)

    # -- receiving ---------------------------------------------------------------------
    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        if self._ws is None:
            raise WebSocketClosedError("not connected")
        try:
            async for raw in self._ws:
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ws.bad_json", sample=str(raw)[:200])
        except websockets.ConnectionClosed as exc:
            raise WebSocketClosedError(str(exc)) from exc


def message_sid_seq(message: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract the subscription id and sequence number, when the message carries them."""
    sid = message.get("sid")
    seq = message.get("seq")
    return (sid if isinstance(sid, int) else None, seq if isinstance(seq, int) else None)


def is_error(message: dict[str, Any]) -> bool:
    return message.get("type") == "error"


def error_code(message: dict[str, Any]) -> int | None:
    body = message.get("msg") or {}
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, int) else None
