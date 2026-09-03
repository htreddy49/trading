"""Live settlement index, read from Kalshi's own feed.

This is the difference between guessing and knowing. Earlier strategies estimated the
settlement price from a crypto exchange, which carries a basis against the index the
contract actually settles on. Kalshi publishes that index itself, including the running
average over the final minute that the contract settles against.

The feed holds one background WebSocket connection and keeps the latest state in memory.
Strategies read it synchronously; nothing in the trading path waits on the network.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

from kalshi_agent.data.base import DataFeed
from kalshi_agent.kalshi.auth import KalshiSigner
from kalshi_agent.kalshi.models import Market
from kalshi_agent.kalshi.ws import CH_INDEX, CH_INDEX_5HZ, KalshiWebSocket, WebSocketClosedError
from kalshi_agent.logging import get_logger

log = get_logger(__name__)

SECONDS_PER_YEAR = 365 * 24 * 3600

# Kalshi index ids, by the asset prefix in the market ticker.
INDEX_FOR_ASSET = {
    "BTC": "BRTI",
    "ETH": "ETHUSD_RTI",
    "SOL": "SOLUSD_RTI",
    "XRP": "XRPUSD_RTI",
    "DOGE": "DOGEUSD_RTI",
}

# Field names carrying each quantity. The exchange chooses these and has changed them;
# match a list rather than one name, and look inside the nested frame.
VALUE_KEYS = ("value_usd", "value", "index_value", "price")
TRAILING_KEYS = ("last_60s_average", "trailing_60s_average", "avg_60s")
WINDOWED_KEYS = (
    "last_60s_windowed_average_15min",
    "windowed_average_15min",
    "quarter_hour_average",
    "windowed_average",
)


def _flat(obj: Any, depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(obj, dict) or depth > 3:
        return out
    for key, value in obj.items():
        if isinstance(value, dict):
            out.update(_flat(value, depth + 1))
        else:
            out[key] = value
    return out


def _pick(fields: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for name in names:
        if fields.get(name) not in (None, ""):
            try:
                return float(fields[name])
            except (TypeError, ValueError):
                continue
    return None


@dataclass(slots=True)
class IndexState:
    """Everything known about one index right now."""

    index_id: str
    value: float
    received_ns: int
    trailing_60s: float | None = None
    windowed_average: float | None = None
    history: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=4000))

    @property
    def age_seconds(self) -> float:
        return (time.time_ns() - self.received_ns) / 1e9

    def realised_vol_annual(
        self,
        *,
        lookback_seconds: float = 900.0,
        bucket_seconds: float = 1.0,
        floor: float = 0.10,
        min_buckets: int = 30,
    ) -> float:
        """Annualised volatility of the index, from its own ticks.

        Estimated from the index rather than from a crypto exchange, because the index is
        built from order book depth across venues and is genuinely smoother than any one
        venue's trades. Using an exchange price would overstate it.

        Ticks are resampled onto a fixed grid before returns are taken. Dividing each
        return by the gap between irregular ticks amplifies noise without limit as that
        gap shrinks, so a burst of closely spaced messages would imply enormous
        volatility and silently stop the strategy trading. A fixed grid cannot do that.
        """
        if len(self.history) < min_buckets:
            return floor
        latest = self.history[-1][0]
        cutoff = latest - lookback_seconds
        buckets: dict[int, float] = {}
        for ts, value in self.history:
            if ts >= cutoff and value > 0:
                buckets[int(ts // bucket_seconds)] = value  # last value wins in each bucket
        if len(buckets) < min_buckets:
            return floor
        rets = [
            math.log(buckets[b] / buckets[b - 1])
            for b in sorted(buckets)
            if b - 1 in buckets and buckets[b - 1] > 0
        ]
        if len(rets) < min_buckets - 1:
            return floor
        mean = fmean(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        per_bucket = math.sqrt(var)
        return max(per_bucket * math.sqrt(SECONDS_PER_YEAR / bucket_seconds), floor)

    def sigma_one_second(self, **kw: Any) -> float:
        """Standard deviation of a one-second move, in price units."""
        return self.value * self.realised_vol_annual(**kw) / math.sqrt(SECONDS_PER_YEAR)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_id": self.index_id,
            "value": self.value,
            "trailing_60s": self.trailing_60s,
            "windowed_average": self.windowed_average,
            "age_s": self.age_seconds,
            "sigma_1s": self.sigma_one_second(),
            "vol_annual": self.realised_vol_annual(),
            "ticks": len(self.history),
        }


class KalshiIndexFeed(DataFeed):
    name = "kalshi_index"

    def __init__(
        self,
        ws_url: str,
        signer: KalshiSigner,
        *,
        index_ids: list[str] | None = None,
        vol_lookback_seconds: float = 900.0,
        vol_safety: float = 1.25,
    ) -> None:
        self.ws_url = ws_url
        self.signer = signer
        self.index_ids = index_ids or ["BRTI"]
        self.vol_lookback_seconds = vol_lookback_seconds
        # Errors here are asymmetric: too much assumed volatility costs trades we would
        # have won, too little costs trades we should never have taken. Bias upward.
        self.vol_safety = vol_safety
        self.state: dict[str, IndexState] = {}
        self._task: asyncio.Task[None] | None = None
        self._channel = CH_INDEX_5HZ
        self._stop = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.ensure_future(self._run())

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            ws = KalshiWebSocket(self.ws_url, self.signer)
            try:
                await ws.connect()
                await ws.subscribe([self._channel], index_ids=self.index_ids)
                backoff = 1.0
                async for message in ws:
                    self.ingest(message, time.time_ns())
                    if self._stop.is_set():
                        return
            except WebSocketClosedError as exc:
                log.warning("index_feed.disconnected", error=str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the feed must survive anything
                log.exception("index_feed.failed", error=str(exc))
            finally:
                await ws.close()
            if self._stop.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    # -- ingest -------------------------------------------------------------------------
    def ingest(self, message: dict[str, Any], received_ns: int) -> None:
        msg_type = message.get("type", "")
        if msg_type == "error" and self._channel == CH_INDEX_5HZ:
            log.warning("index_feed.5hz_unavailable", body=message.get("msg"))
            self._channel = CH_INDEX
            return
        if not msg_type.startswith("cfbenchmarks"):
            return
        fields = _flat(message.get("msg") or {})
        index_id = str(fields.get("index_id") or (self.index_ids[0] if self.index_ids else "?"))
        value = _pick(fields, VALUE_KEYS)
        if value is None:
            return
        state = self.state.get(index_id)
        if state is None:
            state = IndexState(index_id=index_id, value=value, received_ns=received_ns)
            self.state[index_id] = state
        state.value = value
        state.received_ns = received_ns
        state.trailing_60s = _pick(fields, TRAILING_KEYS)
        state.windowed_average = _pick(fields, WINDOWED_KEYS)
        state.history.append((received_ns / 1e9, value))

    # -- feed interface -------------------------------------------------------------------
    async def features(self, market: Market) -> dict[str, Any] | None:
        self.start()
        asset = market.ticker.upper().removeprefix("KX")[:3]
        for prefix, index_id in INDEX_FOR_ASSET.items():
            if market.ticker.upper().startswith(f"KX{prefix}"):
                asset = index_id
                break
        else:
            return None
        state = self.state.get(asset)
        if state is None:
            return None
        data = state.as_dict()
        data["sigma_1s"] = state.sigma_one_second(lookback_seconds=self.vol_lookback_seconds)
        data["sigma_1s_safe"] = data["sigma_1s"] * self.vol_safety
        return data
