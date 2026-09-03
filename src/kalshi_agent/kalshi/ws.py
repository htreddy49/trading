"""Minimal Kalshi WebSocket subscriber.

Streams ``ticker`` / ``orderbook_delta`` / ``trade`` channel messages. Used by the
collector when low-latency data is required; the polling collector is the default
because it is simpler to reason about and sufficient for slow strategies.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import websockets

from kalshi_agent.kalshi.auth import KalshiSigner
from kalshi_agent.logging import get_logger

log = get_logger(__name__)

WS_PATH = "/trade-api/ws/v2"


async def stream(
    url: str,
    signer: KalshiSigner,
    channels: list[str],
    market_tickers: list[str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    headers = signer.headers("GET", WS_PATH)
    async with websockets.connect(url, additional_headers=headers) as ws:
        params: dict[str, Any] = {"channels": channels}
        if market_tickers:
            params["market_tickers"] = market_tickers
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": params}))
        log.info("kalshi.ws.subscribed", channels=channels, tickers=market_tickers)
        async for raw in ws:
            yield json.loads(raw)
