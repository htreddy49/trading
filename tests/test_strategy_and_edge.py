from kalshi_agent.kalshi.models import Side
from kalshi_agent.signals.edge import EdgeDetector
from kalshi_agent.strategy.base import MarketContext, Signal
from kalshi_agent.strategy.registry import get_strategy, list_strategies


def test_registry_lists_builtins():
    assert {"simple_edge", "longshot_fade"} <= set(list_strategies())


def test_simple_edge_no_signal_without_discount(market):
    strat = get_strategy("simple_edge", min_discount=3)
    ctx = MarketContext(market=market, history=[{"mid": 42.0}] * 5)
    assert strat.evaluate(ctx) is None


def test_simple_edge_fires_when_ask_below_fair(market):
    strat = get_strategy("simple_edge", min_discount=3, contracts=7)
    # history says fair value is ~50, current yes ask is 44 => 6c discount on YES
    ctx = MarketContext(market=market, history=[{"mid": 50.0}] * 20)
    sig = strat.evaluate(ctx)
    assert sig is not None
    assert sig.side is Side.YES
    assert sig.limit_price == 44
    assert sig.suggested_contracts == 7
    assert sig.edge > 0


def test_longshot_fade(market):
    market.yes_bid, market.yes_ask, market.no_bid, market.no_ask = 4, 6, 94, 96
    sig = get_strategy("longshot_fade", max_yes_price=10, shrink=0.5).evaluate(
        MarketContext(market=market)
    )
    assert sig is not None and sig.side is Side.NO and sig.limit_price == 96
    assert abs(sig.model_probability - 0.025) < 1e-9


def test_edge_detector_subtracts_fees():
    sig = Signal(
        "T",
        Side.YES,
        model_probability=0.55,
        market_probability=0.5,
        limit_price=50,
        suggested_contracts=10,
    )
    verdict = EdgeDetector(min_edge=0.04).evaluate(sig)
    assert abs(verdict.gross_edge - 0.05) < 1e-9
    assert verdict.fee_per_contract_cents == 1.8  # 18c / 10
    assert abs(verdict.net_edge - 0.032) < 1e-9
    assert not verdict.has_edge
    assert EdgeDetector(min_edge=0.03).evaluate(sig).has_edge


def test_no_side_edge():
    sig = Signal(
        "T",
        Side.NO,
        model_probability=0.2,
        market_probability=0.3,
        limit_price=70,
        suggested_contracts=1,
    )
    assert abs(sig.edge - 0.10) < 1e-9
