import time
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_agent.data.kalshi_index import IndexState, KalshiIndexFeed
from kalshi_agent.kalshi.auth import KalshiSigner, generate_test_key
from kalshi_agent.kalshi.models import Market, Side
from kalshi_agent.strategy.averaging_gap import (
    probability_yes,
    required_move_multiple,
)
from kalshi_agent.strategy.base import MarketContext
from kalshi_agent.strategy.registry import get_strategy

STRIKE = 81_554.0
SIGMA = 7.26  # one-second move at 50% annual vol with BTC near 81.5k
CLOSE = datetime(2026, 9, 3, 22, 15, tzinfo=UTC)


# ------------------------------------------------------------------ the mathematics
def test_required_move_multiple_matches_the_averaging_rule():
    # Each elapsed second is locked in, so the remaining seconds must carry the whole gap.
    assert required_move_multiple(60) == 1.0
    assert required_move_multiple(30) == 2.0
    assert required_move_multiple(15) == 4.0
    assert required_move_multiple(6) == 10.0


def p_no(gap, seconds_left, index_offset=None):
    """Fair price of NO, as a fraction, with the average `gap` below the strike."""
    index = STRIKE - gap if index_offset is None else STRIKE + index_offset
    return 1 - probability_yes(
        running_average=STRIKE - gap,
        strike=STRIKE,
        index=index,
        sigma_one_second=SIGMA,
        seconds_remaining=seconds_left,
    )


def test_certainty_accelerates_as_the_window_closes():
    # r**1.5 in the denominator: certainty does not creep up, it accelerates.
    prices = [p_no(15, r) for r in (40, 30, 20, 15)]
    assert prices == sorted(prices), "later must be more certain"
    assert prices[0] == pytest.approx(0.80, abs=0.02)
    assert prices[-1] > 0.99


def test_bigger_gap_is_more_certain():
    assert p_no(30, 30) > p_no(20, 30) > p_no(10, 30)


def test_at_expiry_it_is_a_step_function():
    assert (
        probability_yes(
            running_average=STRIKE + 1,
            strike=STRIKE,
            index=STRIKE,
            sigma_one_second=SIGMA,
            seconds_remaining=0,
        )
        == 1.0
    )
    assert (
        probability_yes(
            running_average=STRIKE - 1,
            strike=STRIKE,
            index=STRIKE,
            sigma_one_second=SIGMA,
            seconds_remaining=0,
        )
        == 0.0
    )


def test_the_whole_point_index_above_strike_but_average_below():
    """The setup the strategy exists for: the market sees the index, settlement uses the
    average. With the index above the strike a naive trader buys YES; the average says no."""
    fair_no = p_no(gap=21, seconds_left=15, index_offset=+3)  # index 3 above the strike
    assert fair_no > 0.95, "the average cannot catch up, whatever the index just did"


# ------------------------------------------------------------------ the strategy
def market(**kw):
    defaults = dict(
        ticker="KXBTC15M-26SEP031815-15",
        status="active",
        floor_strike=STRIKE,
        close_time=CLOSE,
        yes_bid=40,
        yes_ask=42,
        no_bid=56,
        no_ask=58,
    )
    defaults.update(kw)
    return Market(**defaults)


def ctx(
    *,
    seconds_left=25,
    gap=20.0,
    index_offset=None,
    age=0.2,
    ticks=1000,
    sigma=SIGMA,
    windowed=True,
    **market_kw,
):
    m = market(**market_kw)
    index_value = STRIKE - gap if index_offset is None else STRIKE + index_offset
    feed = {
        "index_id": "BRTI",
        "value": index_value,
        "age_s": age,
        "ticks": ticks,
        "sigma_1s": sigma,
        "sigma_1s_safe": sigma,
        "vol_annual": 0.5,
        "windowed_average": (STRIKE - gap) if windowed else None,
    }
    return MarketContext(
        market=m, now=CLOSE - timedelta(seconds=seconds_left), extra={"kalshi_index": feed}
    )


def strat(**kw):
    return get_strategy("averaging_gap", **kw)


def test_buys_no_when_the_average_is_losing():
    # gap 15 at 30s left -> NO fair about 90c, inside the band; ask 85 leaves real edge
    sig = strat(contracts=5).evaluate(ctx(seconds_left=30, gap=15, no_ask=85))
    assert sig is not None
    assert sig.side is Side.NO and sig.limit_price == 85 and sig.suggested_contracts == 5
    assert sig.features["required_move_multiple"] == pytest.approx(2.0)
    assert sig.features["model_price_c"] == pytest.approx(90.4, abs=0.5)
    assert sig.features["edge_c"] > 3


def test_buys_yes_when_the_average_is_winning():
    sig = strat().evaluate(ctx(seconds_left=30, gap=-15, yes_ask=85))
    assert sig is not None and sig.side is Side.YES and sig.limit_price == 85


def test_declines_outside_the_price_band():
    # Nearly certain: fair price above the band, nothing left to collect for the risk.
    assert strat().evaluate(ctx(seconds_left=15, gap=40, no_ask=90)) is None
    # Genuinely uncertain: fair price below the band, where fees are worst.
    assert strat().evaluate(ctx(seconds_left=40, gap=2, no_ask=40)) is None


def test_declines_when_the_market_already_agrees():
    # Fair value about 90c and the ask is 89c: the edge does not cover fee plus safety.
    assert strat().evaluate(ctx(seconds_left=30, gap=15, no_ask=89)) is None


def test_declines_outside_the_time_window():
    assert strat().evaluate(ctx(seconds_left=90, gap=15, no_ask=85)) is None, "too early"
    assert strat().evaluate(ctx(seconds_left=5, gap=15, no_ask=85)) is None, "too late"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"age": 5.0}, "a stale index is worse than none"),
        ({"ticks": 10}, "too few ticks to estimate volatility"),
        ({"windowed": False}, "not inside the settlement window"),
        ({"sigma": 0.0}, "no volatility estimate"),
    ],
)
def test_abort_conditions(kwargs, reason):
    assert strat().evaluate(ctx(seconds_left=30, gap=15, no_ask=85, **kwargs)) is None, reason


def test_declines_without_a_strike():
    assert strat().evaluate(ctx(seconds_left=30, gap=15, no_ask=85, floor_strike=None)) is None


def test_higher_assumed_volatility_makes_it_trade_less():
    """Errors are asymmetric, so the safety multiplier must only ever reduce activity."""
    assert strat().evaluate(ctx(seconds_left=30, gap=15, no_ask=85, sigma=SIGMA)) is not None
    assert strat().evaluate(ctx(seconds_left=30, gap=15, no_ask=85, sigma=SIGMA * 4)) is None


def test_edge_must_clear_the_fee():
    s = strat(min_edge_cents=0.0, safety_cents=0.0)
    sig = s.evaluate(ctx(seconds_left=30, gap=15, no_ask=85))
    assert sig is not None and sig.features["edge_c"] > sig.features["fee_c"]


# ------------------------------------------------------------------ the index feed
def test_index_state_parses_the_nested_frame():
    feed = KalshiIndexFeed("wss://x", KalshiSigner("k", generate_test_key()))
    feed.ingest(
        {
            "type": "cfbenchmarks_value_5hz",
            "msg": {
                "index_id": "BRTI",
                "value_usd": "81554.23",
                "source_ts_ms": 1,
                "data": {"last_60s_average": "81550.00", "windowed_average_15min": "81549.10"},
            },
        },
        time.time_ns(),
    )
    state = feed.state["BRTI"]
    assert state.value == 81554.23
    assert state.trailing_60s == 81550.00
    assert state.windowed_average == 81549.10


def test_index_feed_falls_back_when_5hz_is_rejected():
    feed = KalshiIndexFeed("wss://x", KalshiSigner("k", generate_test_key()))
    feed.ingest({"type": "error", "msg": {"code": 8}}, time.time_ns())
    assert feed._channel == "cfbenchmarks_value"


def test_realised_vol_uses_the_index_and_has_a_floor():
    state = IndexState(index_id="BRTI", value=81_554.0, received_ns=time.time_ns())
    assert state.realised_vol_annual() == 0.10, "too little history: fall back to the floor"
    t = time.time()
    for i in range(400):  # a dead-flat tape must not imply zero volatility
        state.history.append((t + i * 0.2, 81_554.0))
    assert state.realised_vol_annual() == 0.10
    state.history.clear()
    for i in range(400):
        state.history.append((t + i * 0.2, 81_554.0 + (8.0 if i % 2 else -8.0)))
    vol = state.realised_vol_annual()
    assert vol > 0.10 and state.sigma_one_second() > 0


async def test_feed_returns_none_for_unrelated_markets():
    feed = KalshiIndexFeed("wss://x", KalshiSigner("k", generate_test_key()))
    feed.state["BRTI"] = IndexState("BRTI", 81_554.0, time.time_ns())
    try:
        assert await feed.features(Market(ticker="KXHIGHNY-1", status="open")) is None
        got = await feed.features(market())
        assert got is not None and got["index_id"] == "BRTI"
        assert got["sigma_1s_safe"] >= got["sigma_1s"], "the safety margin only widens it"
    finally:
        await feed.close()


# ------------------------------------------------------------------ volatility estimator
def test_realised_vol_recovers_a_known_volatility():
    """Generate ticks at a known volatility and check the estimator finds it."""
    import math
    import random

    random.seed(5)
    target_annual = 0.50
    sigma_tick = STRIKE * target_annual / math.sqrt(365 * 24 * 3600) * math.sqrt(0.2)
    state = IndexState("BRTI", STRIKE, time.time_ns())
    now, value = time.time() - 600, STRIKE
    for i in range(3000):  # ten minutes at 5 Hz
        value += random.gauss(0, sigma_tick)
        state.history.append((now + i * 0.2, value))
    state.value = value
    assert state.realised_vol_annual() == pytest.approx(target_annual, abs=0.08)


def test_a_burst_of_ticks_cannot_inflate_the_estimate():
    """Ticks microseconds apart must not imply enormous volatility.

    Dividing each return by the gap between irregular ticks amplifies noise without limit
    as that gap shrinks. Resampling onto a fixed grid is what prevents a delivery burst
    from silently stopping the strategy trading.
    """
    state = IndexState("BRTI", STRIKE, time.time_ns())
    now = time.time()
    for i in range(500):
        state.history.append((now + i * 0.00002, STRIKE + (2.0 if i % 2 else -2.0)))
    assert state.realised_vol_annual() == 0.10, "a 10ms burst must fall back to the floor"
