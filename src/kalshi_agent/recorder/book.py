"""In-memory order book rebuilt from a snapshot plus deltas.

Two representation decisions, both load-bearing.

**Prices are integer micro-dollars** (one millionth of a dollar). Kalshi moved from whole
cents to decimal dollars, and these markets use a tapered tick: a tenth of a cent below
ten cents and above ninety, a full cent in between. Storing cents as integers would throw
away exactly the resolution the tails need, and storing dollars as floats would make
equality comparisons on price levels unreliable. One cent is 10,000 micros.

**Quantities are integer hundredths of a contract**, because fractional trading is on and
the minimum order is one hundredth.

The book refuses to answer questions about itself once a sequence gap has been seen. A
stale book that looks healthy is far more dangerous than one that admits it is broken.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

MICROS_PER_DOLLAR = 1_000_000
MICROS_PER_CENT = 10_000
CENTI_PER_CONTRACT = 100


def parse_price(value: Any) -> int:
    """Accept a whole-cent integer or a decimal-dollar string; return micro-dollars.

    Kalshi's older payloads carry ``price`` as an integer number of cents, the current
    ones carry ``price_dollars`` as a fixed-point string such as ``"0.0800"``.
    """
    if isinstance(value, int):
        return value * MICROS_PER_CENT
    return int(Decimal(str(value)) * MICROS_PER_DOLLAR)


def parse_count(value: Any) -> int:
    """Accept a whole-contract integer or a fixed-point string; return hundredths."""
    if isinstance(value, int):
        return value * CENTI_PER_CONTRACT
    return int(Decimal(str(value)) * CENTI_PER_CONTRACT)


def micros_to_cents(micros: int) -> float:
    return micros / MICROS_PER_CENT


def contracts(centi: int) -> float:
    return centi / CENTI_PER_CONTRACT


@dataclass(slots=True, frozen=True)
class PriceLevel:
    price_micros: int
    count_centi: int

    @property
    def price_cents(self) -> float:
        return micros_to_cents(self.price_micros)

    @property
    def contracts(self) -> float:
        return contracts(self.count_centi)


@dataclass(slots=True)
class OrderBook:
    """Resting bids on each side. A YES bid at p is equivalent to a NO ask at 1 - p."""

    ticker: str
    yes: dict[int, int] = field(default_factory=dict)  # price micros -> count centi
    no: dict[int, int] = field(default_factory=dict)
    last_seq: int | None = None
    stale: bool = True  # until the first snapshot arrives
    updates: int = 0

    # -- construction -----------------------------------------------------------------
    def apply_snapshot(self, msg: dict[str, Any], seq: int | None = None) -> None:
        self.yes = self._levels(msg, "yes")
        self.no = self._levels(msg, "no")
        self.last_seq = seq
        self.stale = False
        self.updates += 1

    @staticmethod
    def _levels(msg: dict[str, Any], side: str) -> dict[int, int]:
        # Newest payloads first, then the older shapes, so a format change downgrades
        # gracefully instead of silently producing an empty book.
        for key in (f"{side}_dollars_fp", f"{side}_dollars", side):
            raw = msg.get(key)
            if raw:
                return {parse_price(p): parse_count(q) for p, q in raw}
        return {}

    def apply_delta(self, msg: dict[str, Any], seq: int | None = None) -> None:
        if self.stale:
            return  # nothing to apply a delta to; waiting for a snapshot
        side = msg.get("side")
        book = self.yes if side == "yes" else self.no
        price = parse_price(msg["price_dollars"] if "price_dollars" in msg else msg["price"])
        delta = parse_count(msg["delta_fp"] if "delta_fp" in msg else msg["delta"])
        new_count = book.get(price, 0) + delta
        if new_count > 0:
            book[price] = new_count
        else:
            book.pop(price, None)
        self.last_seq = seq
        self.updates += 1

    def mark_stale(self) -> None:
        """Called on a sequence gap. The book stays unusable until the next snapshot."""
        self.stale = True

    # -- queries ------------------------------------------------------------------------
    @property
    def best_yes_bid(self) -> PriceLevel | None:
        return self._best(self.yes)

    @property
    def best_no_bid(self) -> PriceLevel | None:
        return self._best(self.no)

    @staticmethod
    def _best(side: dict[int, int]) -> PriceLevel | None:
        if not side:
            return None
        price = max(side)
        return PriceLevel(price, side[price])

    @property
    def best_yes_ask_micros(self) -> int | None:
        """The cost of buying YES is one dollar minus the best NO bid.

        Not one minus the YES *ask*: getting this backwards invents arbitrage that is not
        there, and it is the most commonly reported bug in public bots for this exchange.
        """
        best = self.best_no_bid
        return MICROS_PER_DOLLAR - best.price_micros if best else None

    @property
    def best_no_ask_micros(self) -> int | None:
        best = self.best_yes_bid
        return MICROS_PER_DOLLAR - best.price_micros if best else None

    @property
    def spread_micros(self) -> int | None:
        bid, ask = self.best_yes_bid, self.best_yes_ask_micros
        return ask - bid.price_micros if bid and ask is not None else None

    def depth_to_buy(self, side: str, limit_micros: int) -> int:
        """Contracts (in hundredths) available to buy ``side`` at or below ``limit``."""
        resting = self.no if side == "yes" else self.yes
        return sum(
            count for price, count in resting.items() if MICROS_PER_DOLLAR - price <= limit_micros
        )

    def summary(self) -> dict[str, Any]:
        yes_bid, no_bid = self.best_yes_bid, self.best_no_bid
        return {
            "ticker": self.ticker,
            "stale": self.stale,
            "yes_bid_c": yes_bid.price_cents if yes_bid else None,
            "no_bid_c": no_bid.price_cents if no_bid else None,
            "yes_ask_c": micros_to_cents(self.best_yes_ask_micros)
            if self.best_yes_ask_micros is not None
            else None,
            "levels": len(self.yes) + len(self.no),
            "updates": self.updates,
        }
