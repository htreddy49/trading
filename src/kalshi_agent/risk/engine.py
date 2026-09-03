"""Pre-trade risk engine.

Every order -- paper or live -- passes through the same ordered chain of checks::

    kill switch -> exchange open -> edge -> liquidity -> spread -> order size
    -> position limit -> market exposure -> total exposure -> daily loss -> duplicate order

The first failing check rejects the order; all check results are recorded so the
decision log explains *why* something was (not) traded. The engine may also *shrink* the
order to fit inside limits rather than reject it outright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshi_agent.config import Settings
from kalshi_agent.kalshi.models import Orderbook
from kalshi_agent.signals.edge import EdgeVerdict
from kalshi_agent.strategy.base import Signal


@dataclass(slots=True)
class RiskLimits:
    min_edge: float = 0.04
    max_order_contracts: int = 25
    max_position_contracts: int = 100
    max_market_exposure_cents: int = 5_000
    max_total_exposure_cents: int = 50_000
    max_daily_loss_cents: int = 5_000
    min_liquidity_contracts: int = 50
    max_spread_cents: int = 10
    kill_switch_file: Path | None = Path("./KILL_SWITCH")
    duplicate_window_seconds: int = 300

    @classmethod
    def from_settings(cls, s: Settings) -> RiskLimits:
        return cls(
            min_edge=s.risk_min_edge,
            max_order_contracts=s.risk_max_order_contracts,
            max_position_contracts=s.risk_max_position_contracts,
            max_market_exposure_cents=s.risk_max_market_exposure_cents,
            max_total_exposure_cents=s.risk_max_total_exposure_cents,
            max_daily_loss_cents=s.risk_max_daily_loss_cents,
            min_liquidity_contracts=s.risk_min_liquidity_contracts,
            max_spread_cents=s.risk_max_spread_cents,
            kill_switch_file=s.risk_kill_switch_file,
        )


@dataclass(slots=True)
class RiskState:
    """Live portfolio facts the checks need. Supplied by the engine each cycle."""

    position_contracts: int = 0  # net contracts already held in this market (+YES/-NO)
    market_exposure_cents: int = 0
    total_exposure_cents: int = 0
    daily_pnl_cents: int = 0
    exchange_trading_active: bool = True
    orderbook: Orderbook | None = None
    spread_cents: int | None = None
    recent_orders: list[tuple[str, datetime]] = field(default_factory=list)  # (ticker, ts)
    kill_switch_engaged: bool = False


@dataclass(slots=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class RiskVerdict:
    approved: bool
    contracts: int
    checks: list[RiskCheck]

    @property
    def reason(self) -> str:
        failed = [c for c in self.checks if not c.passed]
        return "; ".join(f"{c.name}: {c.detail}" for c in failed) if failed else "approved"

    def as_dicts(self) -> list[dict]:
        return [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks]


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def kill_switch_engaged(self, state: RiskState) -> bool:
        if state.kill_switch_engaged:
            return True
        f = self.limits.kill_switch_file
        return bool(f and Path(f).exists())

    def evaluate(
        self, signal: Signal, edge: EdgeVerdict, state: RiskState, now: datetime | None = None
    ) -> RiskVerdict:
        now = now or datetime.now(UTC)
        lim = self.limits
        checks: list[RiskCheck] = []
        contracts = min(signal.suggested_contracts, lim.max_order_contracts)

        def fail(name: str, detail: str) -> RiskVerdict:
            checks.append(RiskCheck(name, False, detail))
            return RiskVerdict(False, 0, checks)

        def ok(name: str, detail: str = "") -> None:
            checks.append(RiskCheck(name, True, detail))

        # 1. kill switch
        if self.kill_switch_engaged(state):
            return fail("kill_switch", "kill switch engaged")
        ok("kill_switch")

        # 2. exchange
        if not state.exchange_trading_active:
            return fail("exchange_open", "exchange trading is halted")
        ok("exchange_open")

        # 3. edge
        if not edge.has_edge or edge.net_edge < lim.min_edge:
            return fail("edge", edge.reason)
        ok("edge", f"net edge {edge.net_edge:.3f}")

        # 4. liquidity
        if state.orderbook is not None:
            depth = state.orderbook.depth(signal.side, signal.limit_price)
            if depth < lim.min_liquidity_contracts:
                return fail(
                    "liquidity",
                    f"only {depth} contracts at <= {signal.limit_price}c "
                    f"(min {lim.min_liquidity_contracts})",
                )
            ok("liquidity", f"{depth} contracts available")
        else:
            ok("liquidity", "no orderbook supplied; skipped")

        # 5. spread
        if state.spread_cents is not None and state.spread_cents > lim.max_spread_cents:
            return fail("spread", f"spread {state.spread_cents}c > max {lim.max_spread_cents}c")
        ok("spread")

        # 6. order size
        if contracts <= 0:
            return fail("order_size", "zero contracts")
        ok("order_size", f"{contracts} contracts (cap {lim.max_order_contracts})")

        # 7. position limit (same-direction exposure)
        signed = contracts if signal.side.value == "yes" else -contracts
        projected = state.position_contracts + signed
        if abs(projected) > lim.max_position_contracts:
            room = lim.max_position_contracts - abs(state.position_contracts)
            if room <= 0 or (state.position_contracts * signed < 0):
                return fail(
                    "position_limit",
                    f"projected {projected} exceeds max {lim.max_position_contracts}",
                )
            contracts = room
        ok("position_limit", f"projected {state.position_contracts + signed}")

        # 8. market exposure
        order_cost = contracts * signal.limit_price
        if state.market_exposure_cents + order_cost > lim.max_market_exposure_cents:
            room = lim.max_market_exposure_cents - state.market_exposure_cents
            fit = room // signal.limit_price
            if fit <= 0:
                return fail(
                    "market_exposure",
                    f"market exposure {state.market_exposure_cents}c at cap "
                    f"{lim.max_market_exposure_cents}c",
                )
            contracts = min(contracts, fit)
            order_cost = contracts * signal.limit_price
        ok("market_exposure", f"{state.market_exposure_cents + order_cost}c")

        # 9. total exposure
        if state.total_exposure_cents + order_cost > lim.max_total_exposure_cents:
            room = lim.max_total_exposure_cents - state.total_exposure_cents
            fit = room // signal.limit_price
            if fit <= 0:
                return fail(
                    "total_exposure",
                    f"total exposure {state.total_exposure_cents}c at cap "
                    f"{lim.max_total_exposure_cents}c",
                )
            contracts = min(contracts, fit)
            order_cost = contracts * signal.limit_price
        ok("total_exposure", f"{state.total_exposure_cents + order_cost}c")

        # 10. daily loss
        if state.daily_pnl_cents <= -lim.max_daily_loss_cents:
            return fail(
                "daily_loss",
                f"daily P&L {state.daily_pnl_cents}c breaches -{lim.max_daily_loss_cents}c",
            )
        ok("daily_loss", f"daily P&L {state.daily_pnl_cents}c")

        # 11. duplicate order
        window = timedelta(seconds=lim.duplicate_window_seconds)
        for ticker, ts in state.recent_orders:
            if ticker == signal.ticker and now - ts < window:
                return fail("duplicate_order", f"order for {ticker} sent {now - ts} ago")
        ok("duplicate_order")

        return RiskVerdict(True, contracts, checks)
