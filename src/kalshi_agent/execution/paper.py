"""Paper broker: simulates fills against the live orderbook and tracks virtual P&L."""

from __future__ import annotations

import uuid

from kalshi_agent.execution.base import Broker, ExecutionResult
from kalshi_agent.kalshi.models import Action, Orderbook, OrderRequest, Side
from kalshi_agent.portfolio.tracker import Portfolio
from kalshi_agent.signals.fees import kalshi_fee_cents


class PaperBroker(Broker):
    mode = "paper"

    def __init__(
        self, portfolio: Portfolio, *, slippage_cents: int = 1, taker: bool = True
    ) -> None:
        self.portfolio = portfolio
        self.slippage_cents = slippage_cents
        self.taker = taker
        self.orders: dict[str, ExecutionResult] = {}

    async def submit(
        self, request: OrderRequest, orderbook: Orderbook | None = None
    ) -> ExecutionResult:
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        limit = request.limit_price
        if limit is None:
            return self._reject(order_id, "paper broker requires a limit price")

        fills = self._walk_book(request.side, request.action, request.count, limit, orderbook)
        if not fills:
            result = ExecutionResult(order_id, "resting", message="no liquidity at limit; resting")
            self.orders[order_id] = result
            return result

        total_count = sum(c for _, c in fills)
        notional = sum(p * c for p, c in fills)
        avg_price = int(round(notional / total_count))
        # Fee is charged per level (matches Kalshi, which fees each fill separately).
        fees = [kalshi_fee_cents(p, c, is_taker=self.taker) for p, c in fills]
        fee = sum(fees)
        if request.action is Action.BUY and notional + fee > self.portfolio.cash_cents:
            return self._reject(order_id, "insufficient cash")
        try:
            for (price, count), level_fee in zip(fills, fees, strict=True):
                self.portfolio.apply_fill(
                    request.ticker, request.action, request.side, count, price, level_fee
                )
        except ValueError as exc:
            return self._reject(order_id, str(exc))

        status = "filled" if total_count == request.count else "partially_filled"
        result = ExecutionResult(
            order_id,
            status,
            filled_count=total_count,
            avg_price=avg_price,
            fee_cents=fee,
            fills=[{"price": p, "count": c} for p, c in fills],
        )
        self.orders[order_id] = result
        return result

    def _walk_book(
        self, side: Side, action: Action, count: int, limit: int, book: Orderbook | None
    ) -> list[tuple[int, int]]:
        """Return (price, count) fills. With no book, assume full fill at limit + slippage."""
        if book is None:
            price = min(99, max(1, limit if action is Action.SELL else limit + self.slippage_cents))
            if action is Action.BUY and price > limit:
                price = limit  # never pay more than the limit
            return [(price, count)]

        # Buying YES consumes resting NO bids at (100 - no_price); buying NO consumes YES bids.
        if action is Action.BUY:
            resting = book.no if side is Side.YES else book.yes
            levels = sorted(
                ((100 - lvl.price, lvl.quantity) for lvl in resting), key=lambda x: x[0]
            )
            eligible = [(p, q) for p, q in levels if p <= limit]
        else:  # selling our contracts hits bids on the same side
            resting = book.yes if side is Side.YES else book.no
            levels = sorted(((lvl.price, lvl.quantity) for lvl in resting), key=lambda x: -x[0])
            eligible = [(p, q) for p, q in levels if p >= limit]

        fills: list[tuple[int, int]] = []
        remaining = count
        for price, qty in eligible:
            take = min(qty, remaining)
            fills.append((price, take))
            remaining -= take
            if remaining == 0:
                break
        return fills

    async def cancel(self, order_id: str) -> ExecutionResult:
        result = self.orders.get(order_id)
        if result is None:
            return ExecutionResult(order_id, "rejected", message="unknown order")
        if result.status == "resting":
            result.status = "canceled"
        return result

    @staticmethod
    def _reject(order_id: str, message: str) -> ExecutionResult:
        return ExecutionResult(order_id, "rejected", message=message)
