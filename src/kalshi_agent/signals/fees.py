"""Kalshi trading fees.

Kalshi's standard taker fee is ``0.07 * C * P * (1 - P)`` dollars, rounded **up** to the
next cent, where ``C`` is the number of contracts and ``P`` the price in dollars. Maker
orders pay roughly a quarter of that. The rate is configurable because Kalshi has changed
it and applies different multipliers to some series (index markets use 0.035).
"""

from __future__ import annotations

import math

DEFAULT_TAKER_RATE = 0.07
DEFAULT_MAKER_RATE = 0.0175  # roughly a quarter of the taker rate


def kalshi_fee_cents(
    price_cents: int, contracts: int, *, is_taker: bool = True, rate: float | None = None
) -> int:
    if contracts <= 0:
        return 0
    if rate is None:
        rate = DEFAULT_TAKER_RATE if is_taker else DEFAULT_MAKER_RATE
    p = price_cents / 100
    fee_dollars = rate * contracts * p * (1 - p)
    return int(math.ceil(round(fee_dollars * 100, 6)))
