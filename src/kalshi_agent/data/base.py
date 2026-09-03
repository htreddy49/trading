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


def build_feeds(names: set[str] | list[str]) -> list[DataFeed]:
    from kalshi_agent.data.crypto import CoinbaseFeed

    registry: dict[str, type[DataFeed]] = {CoinbaseFeed.name: CoinbaseFeed}
    feeds: list[DataFeed] = []
    for name in dict.fromkeys(names):  # de-dupe, keep order
        try:
            feeds.append(registry[name]())
        except KeyError as exc:
            raise KeyError(f"unknown data feed {name!r}; available: {sorted(registry)}") from exc
    return feeds
