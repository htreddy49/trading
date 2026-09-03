"""Live broker: sends orders to Kalshi.

Guarded twice: the settings validator refuses to construct a live configuration against
production without an explicit acknowledgement, and this class refuses to submit unless
it was constructed with ``armed=True`` by the engine (never by default).
"""

from __future__ import annotations

import uuid

from kalshi_agent.execution.base import Broker, ExecutionResult
from kalshi_agent.kalshi.client import KalshiClient, KalshiError
from kalshi_agent.kalshi.models import Orderbook, OrderRequest
from kalshi_agent.logging import get_logger

log = get_logger(__name__)


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, client: KalshiClient, *, armed: bool = False) -> None:
        self.client = client
        self.armed = armed

    async def submit(
        self, request: OrderRequest, orderbook: Orderbook | None = None
    ) -> ExecutionResult:
        if not self.armed:
            return ExecutionResult("", "rejected", message="live broker is not armed")
        if not request.client_order_id:
            request.client_order_id = f"agent-{uuid.uuid4().hex}"
        try:
            order = await self.client.create_order(request)
        except KalshiError as exc:
            log.error("live.order.rejected", ticker=request.ticker, error=str(exc))
            return ExecutionResult("", "rejected", message=str(exc))
        filled = order.fill_count or 0
        status = (
            "filled"
            if order.count and filled >= order.count
            else ("partially_filled" if filled else order.status)
        )
        log.info(
            "live.order.submitted", order_id=order.order_id, status=status, ticker=order.ticker
        )
        return ExecutionResult(
            order.order_id,
            status,
            filled_count=filled,
            avg_price=request.limit_price,
            message=order.status,
        )

    async def cancel(self, order_id: str) -> ExecutionResult:
        try:
            order = await self.client.cancel_order(order_id)
        except KalshiError as exc:
            return ExecutionResult(order_id, "rejected", message=str(exc))
        return ExecutionResult(order.order_id, "canceled", message=order.status)

    async def close(self) -> None:
        await self.client.close()
