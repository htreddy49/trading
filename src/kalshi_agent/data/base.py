"""External data feeds.

A feed enriches :class:`MarketContext.extra` with features a strategy needs that Kalshi
does not provide (spot prices, volatility, news, ...). Feeds are looked up by name so a
strategy can declare what it needs via ``Strategy.feeds``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from kalshi_agent.kalshi.models import Market


class DataFeed(ABC):
    name: str = "base"

    @abstractmethod
    async def features(self, market: Market) -> dict[str, Any] | None:
        """Return features for this market (stored under ``ctx.extra[self.name]``)."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


def build_feeds(names: set[str] | list[str], **context: Any) -> list[DataFeed]:
    """Construct the named feeds.

    ``context`` supplies what a feed needs but cannot discover for itself; the Kalshi index
    feed needs the websocket url and a request signer.
    """
    from kalshi_agent.data.crypto import CoinbaseFeed
    from kalshi_agent.data.kalshi_index import KalshiIndexFeed

    feeds: list[DataFeed] = []
    for name in dict.fromkeys(names):  # de-dupe, keep order
        if name == CoinbaseFeed.name:
            feeds.append(CoinbaseFeed())
        elif name == KalshiIndexFeed.name:
            ws_url, signer = context.get("ws_url"), context.get("signer")
            if not ws_url or signer is None:
                raise ValueError(
                    "the kalshi_index feed needs Kalshi credentials: the exchange "
                    "authenticates the websocket even for public market data"
                )
            feeds.append(KalshiIndexFeed(ws_url, signer, index_ids=context.get("index_ids")))
        else:
            raise KeyError(f"unknown data feed {name!r}")
    return feeds
