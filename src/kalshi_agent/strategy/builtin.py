"""Built-in strategies.

These are intentionally simple, transparent baselines. Their job is to exercise the whole
pipeline (signal -> edge -> risk -> execution -> P&L) end to end; real alpha comes from
the research loop described in ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

from statistics import fmean

from kalshi_agent.kalshi.models import Side
from kalshi_agent.strategy.base import MarketContext, Signal, Strategy
from kalshi_agent.strategy.registry import register


def _clamp_price(price: float) -> int:
    return max(1, min(99, int(round(price))))


@register
class SimpleEdgeStrategy(Strategy):
    """Fade wide spreads / stale quotes toward a smoothed fair value.

    Fair value = mean of the last ``lookback`` mid prices (or current mid when no history).
    If the ask on either side is at least ``min_discount`` cents cheaper than fair value,
    emit a signal to buy that side at the ask.
    """

    name = "simple_edge"
    version = "0.1.0"

    def __init__(self, lookback: int = 10, min_discount: float = 3.0, contracts: int = 5, **kw):
        super().__init__(lookback=lookback, min_discount=min_discount, contracts=contracts, **kw)
        self.lookback = int(lookback)
        self.min_discount = float(min_discount)
        self.contracts = int(contracts)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        mid = ctx.market.yes_mid
        if mid is None or not ctx.market.is_open:
            return None
        mids = [s["mid"] for s in ctx.history[-self.lookback :] if s.get("mid") is not None]
        fair_yes = fmean(mids + [mid]) if mids else mid
        p_yes = fair_yes / 100
        market_p = mid / 100

        yes_ask, no_ask = ctx.yes_ask, ctx.no_ask
        candidates: list[tuple[float, Side, int]] = []
        if yes_ask is not None:
            candidates.append((fair_yes - yes_ask, Side.YES, yes_ask))
        if no_ask is not None:
            candidates.append(((100 - fair_yes) - no_ask, Side.NO, no_ask))
        if not candidates:
            return None

        discount, side, price = max(candidates, key=lambda c: c[0])
        if discount < self.min_discount:
            return None

        return Signal(
            ticker=ctx.market.ticker,
            side=side,
            model_probability=p_yes,
            market_probability=market_p,
            limit_price=_clamp_price(price),
            suggested_contracts=self.contracts,
            confidence=min(1.0, discount / 10),
            rationale=f"{side.value} ask {price}c is {discount:.1f}c below fair {fair_yes:.1f}c",
            features={"fair_yes": fair_yes, "mid": mid, "n_history": len(mids)},
            strategy=self.name,
            strategy_version=self.version,
        )


@register
class LongshotFadeStrategy(Strategy):
    """Sell longshots: buy NO on markets priced in the tails where favourite-longshot bias
    historically over-prices the unlikely outcome.

    Fires when YES trades below ``max_yes_price`` cents, estimating true P(YES) as
    ``shrink * market_p``.
    """

    name = "longshot_fade"
    version = "0.1.0"

    def __init__(self, max_yes_price: int = 10, shrink: float = 0.6, contracts: int = 5, **kw):
        super().__init__(max_yes_price=max_yes_price, shrink=shrink, contracts=contracts, **kw)
        self.max_yes_price = int(max_yes_price)
        self.shrink = float(shrink)
        self.contracts = int(contracts)

    def evaluate(self, ctx: MarketContext) -> Signal | None:
        mid = ctx.market.yes_mid
        no_ask = ctx.no_ask
        if mid is None or no_ask is None or not ctx.market.is_open or mid > self.max_yes_price:
            return None
        market_p = mid / 100
        p_yes = market_p * self.shrink
        return Signal(
            ticker=ctx.market.ticker,
            side=Side.NO,
            model_probability=p_yes,
            market_probability=market_p,
            limit_price=_clamp_price(no_ask),
            suggested_contracts=self.contracts,
            confidence=0.4,
            rationale=f"longshot fade: YES mid {mid}c, model P(YES)={p_yes:.3f}",
            features={"mid": mid},
            strategy=self.name,
            strategy_version=self.version,
        )
