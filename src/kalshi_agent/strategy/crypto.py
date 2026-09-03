"""Crypto 15-minute strike markets (KXBTC15M, KXETH15M, ...).

Each window Kalshi sets ``floor_strike`` (the price to beat) and the market resolves YES
if the 60-second average of the CF Benchmarks index at the close is at or above it.

Model: treat the reference price as lognormal with zero drift over the remaining time
``tau`` and volatility ``sigma`` (annualised realized vol from 1-minute Coinbase candles)::

    P(YES) = Phi( ln(S / K) / (sigma * sqrt(tau)) )

Then shrink the estimate toward 0.5 by ``shrink`` to account for the basis between
Coinbase spot and the settlement index, the 60-second averaging, and estimation error.
We buy whichever side has the larger positive edge at the current ask.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from kalshi_agent.kalshi.models import Side
from kalshi_agent.strategy.base import MarketContext, Signal, Strategy
from kalshi_agent.strategy.registry import register

SECONDS_PER_YEAR = 365 * 24 * 3600


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def p_above(spot: float, strike: float, sigma_annual: float, seconds_left: float) -> float:
    if seconds_left <= 0 or sigma_annual <= 0:
        return 1.0 if spot >= strike else 0.0
    tau = seconds_left / SECONDS_PER_YEAR
    return norm_cdf(math.log(spot / strike) / (sigma_annual * math.sqrt(tau)))


@register
class Crypto15mStrategy(Strategy):
    """Lognormal probability model for Kalshi 15-minute crypto up/down markets."""

    name = "crypto_15m"
    version = "0.1.0"
    feeds = ("crypto",)

    def __init__(
        self,
        contracts: int = 5,
        shrink: float = 0.85,
        min_seconds_left: int = 90,
        max_seconds_left: int = 14 * 60,
        max_quote_age_seconds: float = 30.0,
        min_edge_hint: float = 0.0,
        **kw: object,
    ) -> None:
        super().__init__(
            contracts=contracts,
            shrink=shrink,
            min_seconds_left=min_seconds_left,
            max_seconds_left=max_seconds_left,
            max_quote_age_seconds=max_quote_age_seconds,
            **kw,
        )
        self.contracts = int(contracts)
        self.shrink = float(shrink)
        self.min_seconds_left = int(min_seconds_left)
        self.max_seconds_left = int(max_seconds_left)
        self.max_quote_age = float(max_quote_age_seconds)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        m = ctx.market
        quote = ctx.extra.get("crypto")
        strike = m.floor_strike if m.floor_strike is not None else m.cap_strike
        if not quote or strike is None or m.close_time is None or not m.is_open:
            return None

        now = ctx.now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        close = m.close_time if m.close_time.tzinfo else m.close_time.replace(tzinfo=UTC)
        seconds_left = (close - now).total_seconds()
        if not (self.min_seconds_left <= seconds_left <= self.max_seconds_left):
            return None
        if now.timestamp() - float(quote["ts"]) > self.max_quote_age:
            return None

        spot, sigma = float(quote["spot"]), float(quote["sigma_annual"])
        raw = p_above(spot, float(strike), sigma, seconds_left)
        if m.floor_strike is None and m.cap_strike is not None:  # "below" style market
            raw = 1 - raw
        p_yes = 0.5 + (raw - 0.5) * self.shrink

        market_p = ctx.market_probability
        if market_p is None:
            return None

        yes_ask, no_ask = ctx.yes_ask, ctx.no_ask
        candidates: list[tuple[float, Side, int]] = []
        if yes_ask is not None:
            candidates.append((p_yes - yes_ask / 100, Side.YES, yes_ask))
        if no_ask is not None:
            candidates.append(((1 - p_yes) - no_ask / 100, Side.NO, no_ask))
        if not candidates:
            return None
        edge, side, price = max(candidates, key=lambda c: c[0])
        if edge <= 0 or not 1 <= price <= 99:
            return None

        return Signal(
            ticker=m.ticker,
            side=side,
            model_probability=p_yes,
            market_probability=market_p,
            limit_price=int(price),
            suggested_contracts=self.contracts,
            confidence=min(1.0, abs(p_yes - 0.5) * 2),
            rationale=(
                f"spot {spot:,.2f} vs strike {strike:,.2f}, sigma {sigma:.2f}, "
                f"{seconds_left:.0f}s left -> P(YES)={p_yes:.3f}; {side.value} ask {price}c"
            ),
            features={
                "spot": spot,
                "strike": float(strike),
                "sigma_annual": sigma,
                "seconds_left": seconds_left,
                "p_raw": raw,
                "p_yes": p_yes,
                "quote_age_s": now.timestamp() - float(quote["ts"]),
            },
            strategy=self.name,
            strategy_version=self.version,
        )
