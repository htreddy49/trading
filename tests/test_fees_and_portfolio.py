import pytest

from kalshi_agent.kalshi.models import Action, Side
from kalshi_agent.portfolio.tracker import Portfolio
from kalshi_agent.signals.fees import kalshi_fee_cents


@pytest.mark.parametrize(
    "price,count,expected",
    [
        (50, 1, 2),  # 0.07 * 0.25 = 1.75c -> 2c
        (50, 100, 175),
        (10, 100, 63),  # 0.07*100*0.1*0.9 = 0.63 -> 63c
        (99, 1, 1),  # tiny but rounds up to 1c
        (50, 0, 0),
    ],
)
def test_kalshi_fee(price, count, expected):
    assert kalshi_fee_cents(price, count) == expected


def test_maker_fee_zero_by_default():
    assert kalshi_fee_cents(50, 100, is_taker=False) == 0


def test_buy_yes_then_settle_yes():
    pf = Portfolio(cash_cents=10_000)
    pf.apply_fill("T", Action.BUY, Side.YES, 10, 40, fee=2)
    assert pf.cash_cents == 10_000 - 400 - 2
    assert pf.exposure_cents == 400
    assert pf.unrealized_pnl_cents({"T": 50}) == 100
    realized = pf.settle("T", "yes")
    assert realized == 600
    assert pf.cash_cents == 10_000 - 2 + 600
    assert pf.exposure_cents == 0


def test_buy_no_then_settle_yes_loses_cost():
    pf = Portfolio(cash_cents=10_000)
    pf.apply_fill("T", Action.BUY, Side.NO, 5, 60, fee=0)
    assert pf.settle("T", "yes") == -300
    assert pf.cash_cents == 9_700


def test_sell_realizes_pnl():
    pf = Portfolio(cash_cents=10_000)
    pf.apply_fill("T", Action.BUY, Side.YES, 10, 40, fee=0)
    pf.apply_fill("T", Action.SELL, Side.YES, 4, 50, fee=1)
    pos = pf.positions["T"]
    assert pos.yes_contracts == 6
    assert pos.realized_pnl_cents == 40
    assert pf.cash_cents == 10_000 - 400 + 200 - 1


def test_insufficient_cash():
    pf = Portfolio(cash_cents=100)
    with pytest.raises(ValueError, match="insufficient cash"):
        pf.apply_fill("T", Action.BUY, Side.YES, 10, 40, fee=0)


def test_cannot_oversell():
    pf = Portfolio(cash_cents=10_000)
    pf.apply_fill("T", Action.BUY, Side.YES, 2, 40, fee=0)
    with pytest.raises(ValueError):
        pf.apply_fill("T", Action.SELL, Side.YES, 3, 40, fee=0)
