"""The averaging gap.

These markets settle on the average of the index over the final sixty seconds, not on its
price. An average has memory: thirty seconds in, half the final number is already fixed
and no later move can change it. So the index can cross the strike late in the window and
the market will reprice as though the outcome flipped, while the settlement statistic has
not moved nearly far enough to follow.

With ``A`` the running average of the elapsed part of the settlement window, ``K`` the
strike, ``S`` the current index and ``r`` the seconds remaining::

    the remaining seconds must average   K + (K - A) * (60 - r) / r
    which is a move from here of         M = that - S
    the average of r seconds varies by   sigma_1s * sqrt(r / 3)
    so                                   P(YES) = Phi(-M / that)

Nothing is fitted. ``sigma_1s`` is the only estimate, and the model is violently sensitive
to it, so the volatility used here is deliberately biased upward and the strategy refuses
to act when the estimate is thin.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from kalshi_agent.kalshi.models import Side
from kalshi_agent.signals.fees import kalshi_fee_cents
from kalshi_agent.strategy.base import MarketContext, Signal, Strategy
from kalshi_agent.strategy.registry import register

SETTLEMENT_WINDOW_SECONDS = 60.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def probability_yes(
    *,
    running_average: float,
    strike: float,
    index: float,
    sigma_one_second: float,
    seconds_remaining: float,
    window_seconds: float = SETTLEMENT_WINDOW_SECONDS,
) -> float:
    """Probability the settlement average finishes at or above the strike."""
    r = seconds_remaining
    if r <= 0:
        return 1.0 if running_average >= strike else 0.0
    if sigma_one_second <= 0:
        return 1.0 if index >= strike else 0.0
    elapsed = max(window_seconds - r, 0.0)
    deficit = strike - running_average
    required_average = strike + deficit * elapsed / r
    move_needed = required_average - index
    spread = sigma_one_second * math.sqrt(r / 3.0)
    return norm_cdf(-move_needed / spread)


def required_move_multiple(seconds_remaining: float, window_seconds: float = 60.0) -> float:
    """How many times the current gap the index must move to flip the outcome."""
    return window_seconds / seconds_remaining if seconds_remaining > 0 else math.inf


@register
class AveragingGapStrategy(Strategy):
    """Trade the divergence between the index and the average that settles the contract."""

    name = "averaging_gap"
    version = "0.1.0"
    feeds = ("kalshi_index",)

    def __init__(
        self,
        contracts: int = 5,
        min_seconds_left: float = 15.0,
        max_seconds_left: float = 40.0,
        min_model_price_cents: float = 85.0,
        max_model_price_cents: float = 96.0,
        min_edge_cents: float = 3.0,
        safety_cents: float = 2.0,
        max_index_age_seconds: float = 2.0,
        min_index_ticks: int = 300,
        **kw: Any,
    ) -> None:
        super().__init__(
            contracts=contracts,
            min_seconds_left=min_seconds_left,
            max_seconds_left=max_seconds_left,
            min_model_price_cents=min_model_price_cents,
            max_model_price_cents=max_model_price_cents,
            min_edge_cents=min_edge_cents,
            safety_cents=safety_cents,
            max_index_age_seconds=max_index_age_seconds,
            min_index_ticks=min_index_ticks,
            **kw,
        )
        self.contracts = int(contracts)
        self.min_seconds_left = float(min_seconds_left)
        self.max_seconds_left = float(max_seconds_left)
        self.min_model_price = float(min_model_price_cents)
        self.max_model_price = float(max_model_price_cents)
        self.min_edge_cents = float(min_edge_cents)
        self.safety_cents = float(safety_cents)
        self.max_index_age = float(max_index_age_seconds)
        self.min_index_ticks = int(min_index_ticks)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        m = ctx.market
        index = ctx.extra.get("kalshi_index")
        strike = m.floor_strike if m.floor_strike is not None else m.cap_strike
        if not index or strike is None or m.close_time is None or not m.is_open:
            return None

        now = ctx.now or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        close = m.close_time if m.close_time.tzinfo else m.close_time.replace(tzinfo=UTC)
        seconds_left = (close - now).total_seconds()
        if not (self.min_seconds_left <= seconds_left <= self.max_seconds_left):
            return None

        # Abort conditions. Each of these is a way to be confidently wrong.
        if float(index.get("age_s", 99)) > self.max_index_age:
            return None
        if int(index.get("ticks", 0)) < self.min_index_ticks:
            return None  # volatility estimate is too thin to trust
        running_average = index.get("windowed_average")
        if running_average is None:
            return None  # not inside the settlement window yet, or the field is absent
        sigma = float(index.get("sigma_1s_safe") or index.get("sigma_1s") or 0.0)
        if sigma <= 0:
            return None

        p_yes = probability_yes(
            running_average=float(running_average),
            strike=float(strike),
            index=float(index["value"]),
            sigma_one_second=sigma,
            seconds_remaining=seconds_left,
        )
        if m.floor_strike is None and m.cap_strike is not None:  # "below" style market
            p_yes = 1 - p_yes

        # Trade only where the certainty is high enough to be worth the risk and the fee is
        # small. Outside this band either the outcome is genuinely uncertain and fees are at
        # their worst, or there is nothing left to collect.
        favoured, model_price = (
            (Side.YES, p_yes * 100) if p_yes >= 0.5 else (Side.NO, (1 - p_yes) * 100)
        )
        if not (self.min_model_price <= model_price <= self.max_model_price):
            return None

        ask = ctx.yes_ask if favoured is Side.YES else ctx.no_ask
        if ask is None or not 1 <= ask <= 99:
            return None

        fee = kalshi_fee_cents(int(ask), self.contracts) / max(self.contracts, 1)
        edge = model_price - ask
        if edge < max(self.min_edge_cents, fee + self.safety_cents):
            return None

        market_p = ctx.market_probability
        if market_p is None:
            return None

        return Signal(
            ticker=m.ticker,
            side=favoured,
            model_probability=p_yes,
            market_probability=market_p,
            limit_price=int(ask),
            suggested_contracts=self.contracts,
            confidence=min(1.0, edge / 10),
            rationale=(
                f"avg {running_average:,.2f} vs strike {strike:,.2f}, index {index['value']:,.2f}, "
                f"{seconds_left:.0f}s left needs {required_move_multiple(seconds_left):.1f}x the "
                f"gap; model {model_price:.1f}c vs ask {ask}c, "
                f"edge {edge:.1f}c after {fee:.2f}c fee"
            ),
            features={
                "running_average": float(running_average),
                "strike": float(strike),
                "index": float(index["value"]),
                "gap": float(strike) - float(running_average),
                "seconds_left": seconds_left,
                "sigma_1s": sigma,
                "vol_annual": index.get("vol_annual"),
                "model_price_c": model_price,
                "ask_c": ask,
                "edge_c": edge,
                "fee_c": fee,
                "required_move_multiple": required_move_multiple(seconds_left),
            },
            strategy=self.name,
            strategy_version=self.version,
        )
