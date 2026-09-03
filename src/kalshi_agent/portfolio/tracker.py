"""In-memory portfolio accounting shared by paper trading and backtesting.

Kalshi contracts pay $1 (100c) if the side wins, else 0. Buying YES at ``p`` costs ``p``;
buying NO at ``q`` costs ``q``. Holding both YES and NO in the same market is netted
because 1 YES + 1 NO always pays exactly 100c.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_agent.kalshi.models import Action, Side


@dataclass(slots=True)
class PortfolioPosition:
    ticker: str
    yes_contracts: int = 0
    no_contracts: int = 0
    yes_cost_cents: int = 0  # total cost basis of open YES contracts
    no_cost_cents: int = 0
    realized_pnl_cents: int = 0
    fees_cents: int = 0

    @property
    def net(self) -> int:
        return self.yes_contracts - self.no_contracts

    @property
    def exposure_cents(self) -> int:
        return self.yes_cost_cents + self.no_cost_cents

    def avg_cost(self, side: Side) -> float:
        n = self.yes_contracts if side is Side.YES else self.no_contracts
        cost = self.yes_cost_cents if side is Side.YES else self.no_cost_cents
        return cost / n if n else 0.0

    def unrealized_pnl_cents(self, yes_mark: int | None) -> int:
        if yes_mark is None:
            return 0
        no_mark = 100 - yes_mark
        return (
            self.yes_contracts * yes_mark
            - self.yes_cost_cents
            + self.no_contracts * no_mark
            - self.no_cost_cents
        )

    def apply_fill(self, action: Action, side: Side, count: int, price: int, fee: int) -> int:
        """Apply a fill and return realized P&L delta (cents, before fees)."""
        self.fees_cents += fee
        realized = 0
        if action is Action.BUY:
            if side is Side.YES:
                self.yes_contracts += count
                self.yes_cost_cents += count * price
            else:
                self.no_contracts += count
                self.no_cost_cents += count * price
        else:  # SELL: closing contracts at ``price``
            if side is Side.YES:
                if count > self.yes_contracts:
                    raise ValueError("cannot sell more YES than held")
                avg = self.avg_cost(Side.YES)
                realized = int(round(count * (price - avg)))
                self.yes_cost_cents -= int(round(count * avg))
                self.yes_contracts -= count
            else:
                if count > self.no_contracts:
                    raise ValueError("cannot sell more NO than held")
                avg = self.avg_cost(Side.NO)
                realized = int(round(count * (price - avg)))
                self.no_cost_cents -= int(round(count * avg))
                self.no_contracts -= count
        self.realized_pnl_cents += realized
        return realized

    def settle(self, result: str) -> int:
        """Settle the market (``result`` = 'yes' or 'no'), return realized delta."""
        yes_payout = 100 if result == "yes" else 0
        realized = (
            self.yes_contracts * yes_payout
            - self.yes_cost_cents
            + self.no_contracts * (100 - yes_payout)
            - self.no_cost_cents
        )
        self.realized_pnl_cents += realized
        self.yes_contracts = self.no_contracts = 0
        self.yes_cost_cents = self.no_cost_cents = 0
        return realized


@dataclass(slots=True)
class Portfolio:
    cash_cents: int
    positions: dict[str, PortfolioPosition] = field(default_factory=dict)
    starting_cash_cents: int = 0

    def __post_init__(self) -> None:
        if not self.starting_cash_cents:
            self.starting_cash_cents = self.cash_cents

    def position(self, ticker: str) -> PortfolioPosition:
        return self.positions.setdefault(ticker, PortfolioPosition(ticker))

    def net_position(self, ticker: str) -> int:
        pos = self.positions.get(ticker)
        return pos.net if pos else 0

    @property
    def exposure_cents(self) -> int:
        return sum(p.exposure_cents for p in self.positions.values())

    @property
    def realized_pnl_cents(self) -> int:
        return sum(p.realized_pnl_cents for p in self.positions.values())

    @property
    def fees_cents(self) -> int:
        return sum(p.fees_cents for p in self.positions.values())

    def unrealized_pnl_cents(self, marks: dict[str, int | None]) -> int:
        return sum(p.unrealized_pnl_cents(marks.get(t)) for t, p in self.positions.items())

    def equity_cents(self, marks: dict[str, int | None]) -> int:
        return self.cash_cents + self.exposure_cents + self.unrealized_pnl_cents(marks)

    def apply_fill(self, ticker: str, action: Action, side: Side, count: int, price: int, fee: int):
        pos = self.position(ticker)
        cost = count * price
        if action is Action.BUY:
            if cost + fee > self.cash_cents:
                raise ValueError("insufficient cash")
            self.cash_cents -= cost + fee
            pos.apply_fill(action, side, count, price, fee)
        else:
            pos.apply_fill(action, side, count, price, fee)
            self.cash_cents += cost - fee

    def settle(self, ticker: str, result: str) -> int:
        pos = self.positions.get(ticker)
        if pos is None:
            return 0
        payout = (pos.yes_contracts * 100 if result == "yes" else 0) + (
            pos.no_contracts * 100 if result == "no" else 0
        )
        realized = pos.settle(result)
        self.cash_cents += payout
        return realized
