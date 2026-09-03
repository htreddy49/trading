"""Crypto spot prices and realized volatility from Coinbase's public API (no key needed).

Kalshi crypto markets settle on CF Benchmarks real-time indices, which aggregate several
exchanges including Coinbase, so Coinbase spot is a close proxy. Quotes are cached briefly
so a 500-market cycle does not hammer the API.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from statistics import fmean
from typing import Any

import httpx

from kalshi_agent.data.base import DataFeed
from kalshi_agent.kalshi.models import Market
from kalshi_agent.logging import get_logger

log = get_logger(__name__)

COINBASE_URL = "https://api.exchange.coinbase.com"
MINUTES_PER_YEAR = 365 * 24 * 60

# Kalshi series prefix -> Coinbase product
ASSET_PRODUCTS: dict[str, str] = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
    "DOGE": "DOGE-USD",
}
_ASSET_RE = re.compile(r"^KX(BTC|ETH|SOL|XRP|DOGE)")


def asset_for_market(market: Market) -> str | None:
    """Map a Kalshi ticker like ``KXBTC15M-26APR100545-45`` to ``BTC``."""
    m = _ASSET_RE.match(market.ticker.upper())
    return m.group(1) if m else None


@dataclass(slots=True)
class CryptoQuote:
    product: str
    spot: float
    ts: float
    sigma_annual: float  # annualised volatility of log returns
    minute_returns: int  # sample size behind sigma

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "spot": self.spot,
            "ts": self.ts,
            "sigma_annual": self.sigma_annual,
            "minute_returns": self.minute_returns,
        }


class CoinbaseFeed(DataFeed):
    name = "crypto"

    def __init__(
        self,
        *,
        base_url: str = COINBASE_URL,
        spot_ttl_seconds: float = 5.0,
        vol_ttl_seconds: float = 60.0,
        vol_window_minutes: int = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.spot_ttl = spot_ttl_seconds
        self.vol_ttl = vol_ttl_seconds
        self.vol_window = vol_window_minutes
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=5.0,
            transport=transport,
            headers={"User-Agent": "kalshi-agent"},
        )
        self._spot: dict[str, tuple[float, float]] = {}  # product -> (price, ts)
        self._vol: dict[str, tuple[float, int, float]] = {}  # product -> (sigma, n, ts)

    async def close(self) -> None:
        await self._client.aclose()

    # -- public ---------------------------------------------------------------------
    async def quote(self, product: str) -> CryptoQuote:
        spot, ts = await self._get_spot(product)
        sigma, n = await self._get_vol(product)
        return CryptoQuote(product=product, spot=spot, ts=ts, sigma_annual=sigma, minute_returns=n)

    async def features(self, market: Market) -> dict[str, Any] | None:
        asset = asset_for_market(market)
        if asset is None or asset not in ASSET_PRODUCTS:
            return None
        try:
            return (await self.quote(ASSET_PRODUCTS[asset])).as_dict()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("crypto.feed_error", product=ASSET_PRODUCTS[asset], error=str(exc))
            return None

    # -- internals --------------------------------------------------------------------
    async def _get_spot(self, product: str) -> tuple[float, float]:
        now = time.time()
        cached = self._spot.get(product)
        if cached and now - cached[1] < self.spot_ttl:
            return cached
        r = await self._client.get(f"/products/{product}/ticker")
        r.raise_for_status()
        price = float(r.json()["price"])
        self._spot[product] = (price, now)
        return price, now

    async def _get_vol(self, product: str) -> tuple[float, int]:
        now = time.time()
        cached = self._vol.get(product)
        if cached and now - cached[2] < self.vol_ttl:
            return cached[0], cached[1]
        r = await self._client.get(f"/products/{product}/candles", params={"granularity": 60})
        r.raise_for_status()
        # candles: [time, low, high, open, close, volume], newest first
        closes = [float(c[4]) for c in r.json()][: self.vol_window]
        closes.reverse()
        sigma, n = realized_vol_annualised(closes)
        self._vol[product] = (sigma, n, now)
        return sigma, n


def realized_vol_annualised(closes: list[float], floor: float = 0.15) -> tuple[float, int]:
    """Annualised std-dev of 1-minute log returns. ``floor`` guards against dead tape."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0]
    n = len(rets)
    if n < 10:
        return floor, n
    mean = fmean(rets)
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    sigma = math.sqrt(var) * math.sqrt(MINUTES_PER_YEAR)
    return max(sigma, floor), n
