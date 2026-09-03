from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from kalshi_agent.kalshi.models import Orderbook, OrderRequest


@dataclass(slots=True)
class ExecutionResult:
    order_id: str
    status: str  # filled | resting | rejected | canceled
    filled_count: int = 0
    avg_price: int | None = None
    fee_cents: int = 0
    message: str = ""
    fills: list[dict] = field(default_factory=list)


class Broker(ABC):
    mode: str = "base"

    @abstractmethod
    async def submit(
        self, request: OrderRequest, orderbook: Orderbook | None = None
    ) -> ExecutionResult: ...

    @abstractmethod
    async def cancel(self, order_id: str) -> ExecutionResult: ...

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None
