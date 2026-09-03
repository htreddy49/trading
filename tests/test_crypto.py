from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from kalshi_agent.data.crypto import CoinbaseFeed, asset_for_market, realized_vol_annualised
from kalshi_agent.kalshi.models import Market, Side
from kalshi_agent.strategy.base import MarketContext
from kalshi_agent.strategy.crypto import p_above
from kalshi_agent.strategy.registry import get_strategy

CB = "https://api.exchange.coinbase.com"


def crypto_market(strike: float = 110_000.0, yes_ask: int = 50, no_ask: int = 52) -> Market:
    return Market(
        ticker="KXBTC15M-26SEP031415-T110000",
        status="active",
        yes_bid=yes_ask - 2,
        yes_ask=yes_ask,
        no_bid=no_ask - 2,
        no_ask=no_ask,
        floor_strike=strike,
        close_time=datetime(2026, 9, 3, 14, 15, tzinfo=UTC),
    )


def ctx_at(
    market: Market, *, spot: float, sigma: float = 0.5, seconds_left: int = 600, age: float = 0.0
):
    now = market.close_time - timedelta(seconds=seconds_left)
    return MarketContext(
        market=market,
        now=now,
        extra={
            "crypto": {
                "spot": spot,
                "sigma_annual": sigma,
                "ts": now.timestamp() - age,
                "product": "BTC-USD",
                "minute_returns": 100,
            }
        },
    )


def test_asset_detection():
    assert asset_for_market(crypto_market()) == "BTC"
    assert asset_for_market(Market(ticker="KXETH15M-X", status="open")) == "ETH"
    assert asset_for_market(Market(ticker="KXHIGHNY-25", status="open")) is None


def test_p_above_monotonic_and_bounded():
    assert p_above(110_000, 110_000, 0.5, 600) == pytest.approx(0.5)
    assert p_above(112_000, 110_000, 0.5, 600) > 0.9
    assert p_above(108_000, 110_000, 0.5, 600) < 0.1
    # at expiry it collapses to a step function
    assert p_above(110_001, 110_000, 0.5, 0) == 1.0
    assert p_above(109_999, 110_000, 0.5, 0) == 0.0
    # less time left => more certainty
    assert p_above(110_500, 110_000, 0.5, 60) > p_above(110_500, 110_000, 0.5, 600)


def test_strategy_buys_yes_when_spot_far_above_strike():
    strat = get_strategy("crypto_15m", contracts=3, shrink=1.0)
    sig = strat.evaluate(ctx_at(crypto_market(yes_ask=50), spot=111_500))
    assert sig is not None and sig.side is Side.YES
    assert sig.model_probability > 0.9 and sig.limit_price == 50 and sig.suggested_contracts == 3
    assert sig.edge > 0.4


def test_strategy_buys_no_when_spot_far_below_strike():
    sig = get_strategy("crypto_15m", shrink=1.0).evaluate(
        ctx_at(crypto_market(no_ask=50), spot=108_500)
    )
    assert sig is not None and sig.side is Side.NO and sig.limit_price == 50


def test_no_signal_when_market_is_fairly_priced():
    # spot == strike => P(YES) = 0.5; both asks at 52c leave no edge
    assert (
        get_strategy("crypto_15m").evaluate(
            ctx_at(crypto_market(yes_ask=52, no_ask=52), spot=110_000)
        )
        is None
    )


def test_shrink_pulls_probability_toward_half():
    market, spot = crypto_market(), 111_500.0
    full = get_strategy("crypto_15m", shrink=1.0).evaluate(ctx_at(market, spot=spot))
    shrunk = get_strategy("crypto_15m", shrink=0.5).evaluate(ctx_at(market, spot=spot))
    assert full.model_probability > shrunk.model_probability > 0.5


def test_skips_outside_time_window_and_on_stale_quotes():
    strat = get_strategy("crypto_15m", min_seconds_left=90, max_seconds_left=840)
    m = crypto_market()
    assert strat.evaluate(ctx_at(m, spot=111_500, seconds_left=30)) is None  # too close
    assert strat.evaluate(ctx_at(m, spot=111_500, seconds_left=900)) is None  # too early
    assert strat.evaluate(ctx_at(m, spot=111_500, age=120)) is None  # stale quote


def test_skips_without_feed_data_or_strike():
    strat = get_strategy("crypto_15m")
    assert strat.evaluate(MarketContext(market=crypto_market(), now=datetime.now(UTC))) is None
    m = crypto_market()
    m.floor_strike = None
    assert strat.evaluate(ctx_at(m, spot=111_500)) is None


def test_realized_vol():
    sigma, n = realized_vol_annualised([100.0] * 5)
    assert sigma == 0.15 and n == 4  # too few samples -> floor
    closes = [100 * (1.0002 if i % 2 else 0.9998) for i in range(120)]
    sigma, n = realized_vol_annualised(closes)
    assert n == 119 and sigma > 0.15


@respx.mock
async def test_coinbase_feed_fetches_and_caches():
    ticker = respx.get(f"{CB}/products/BTC-USD/ticker").mock(
        return_value=httpx.Response(200, json={"price": "111234.56"})
    )
    candles = respx.get(f"{CB}/products/BTC-USD/candles").mock(
        return_value=httpx.Response(
            200, json=[[i, 1, 2, 3, 110_000 + (i % 7) * 30, 5] for i in range(200)]
        )
    )
    feed = CoinbaseFeed()
    try:
        features = await feed.features(crypto_market())
        assert features["spot"] == 111234.56
        assert features["sigma_annual"] > 0
        assert features["minute_returns"] == 119
        await feed.features(crypto_market())  # cached
    finally:
        await feed.close()
    assert ticker.call_count == 1 and candles.call_count == 1


@respx.mock
async def test_feed_returns_none_for_non_crypto_and_on_error():
    feed = CoinbaseFeed()
    try:
        assert await feed.features(Market(ticker="KXHIGHNY-1", status="open")) is None
        respx.get(f"{CB}/products/BTC-USD/ticker").mock(return_value=httpx.Response(500))
        assert await feed.features(crypto_market()) is None
    finally:
        await feed.close()
