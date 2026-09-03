"""Edge detector: "is there edge after fees?"

Turns a raw strategy signal into an expected-value estimate per contract, net of fees,
and answers yes/no against the configured minimum edge.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_agent.signals.fees import kalshi_fee_cents
from kalshi_agent.strategy.base import Signal


@dataclass(slots=True)
class EdgeVerdict:
    has_edge: bool
    gross_edge: float  # probability points
    fee_per_contract_cents: float
    net_edge: float  # probability points net of fees
    expected_value_cents: float  # per contract
    reason: str


class EdgeDetector:
    def __init__(self, min_edge: float = 0.04, taker: bool = True) -> None:
        self.min_edge = min_edge
        self.taker = taker

    def evaluate(self, signal: Signal) -> EdgeVerdict:
        gross = signal.edge
        fee = kalshi_fee_cents(signal.limit_price, signal.suggested_contracts, is_taker=self.taker)
        fee_per_contract = fee / max(signal.suggested_contracts, 1)
        net = gross - fee_per_contract / 100
        ev = net * 100
        if net >= self.min_edge:
            return EdgeVerdict(True, gross, fee_per_contract, net, ev, "edge above threshold")
        return EdgeVerdict(
            False,
            gross,
            fee_per_contract,
            net,
            ev,
            f"net edge {net:.3f} below minimum {self.min_edge:.3f}",
        )
