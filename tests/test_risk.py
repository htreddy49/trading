from datetime import UTC, datetime, timedelta

import pytest

from kalshi_agent.kalshi.models import Side
from kalshi_agent.risk.engine import RiskEngine, RiskLimits, RiskState
from kalshi_agent.signals.edge import EdgeDetector
from kalshi_agent.strategy.base import Signal


@pytest.fixture
def signal():
    return Signal(
        "T",
        Side.YES,
        model_probability=0.60,
        market_probability=0.45,
        limit_price=44,
        suggested_contracts=10,
    )


@pytest.fixture
def edge(signal):
    return EdgeDetector(min_edge=0.04).evaluate(signal)


@pytest.fixture
def engine():
    return RiskEngine(
        RiskLimits(
            kill_switch_file=None,
            max_order_contracts=25,
            max_position_contracts=30,
            max_market_exposure_cents=2000,
            max_total_exposure_cents=5000,
            max_daily_loss_cents=1000,
            min_liquidity_contracts=50,
            max_spread_cents=10,
        )
    )


def failed(verdict):
    return [c.name for c in verdict.checks if not c.passed]


def test_approves_clean_signal(engine, signal, edge, orderbook):
    v = engine.evaluate(signal, edge, RiskState(orderbook=orderbook, spread_cents=4))
    assert v.approved and v.contracts == 10
    assert all(c.passed for c in v.checks)


def test_kill_switch_blocks_everything(engine, signal, edge):
    v = engine.evaluate(signal, edge, RiskState(kill_switch_engaged=True))
    assert not v.approved and failed(v) == ["kill_switch"]


def test_kill_switch_file(tmp_path, signal, edge):
    f = tmp_path / "KILL"
    eng = RiskEngine(RiskLimits(kill_switch_file=f))
    assert eng.evaluate(signal, edge, RiskState()).approved
    f.write_text("x")
    assert failed(eng.evaluate(signal, edge, RiskState())) == ["kill_switch"]


def test_exchange_halted(engine, signal, edge):
    assert failed(engine.evaluate(signal, edge, RiskState(exchange_trading_active=False))) == [
        "exchange_open"
    ]


def test_no_edge(engine, signal):
    weak = EdgeDetector(min_edge=0.5).evaluate(signal)
    assert failed(engine.evaluate(signal, weak, RiskState())) == ["edge"]


def test_liquidity(engine, signal, edge, orderbook):
    orderbook.no[1].quantity = 10  # only 10 contracts at yes ask 44
    v = engine.evaluate(signal, edge, RiskState(orderbook=orderbook))
    assert failed(v) == ["liquidity"]


def test_spread(engine, signal, edge):
    assert failed(engine.evaluate(signal, edge, RiskState(spread_cents=15))) == ["spread"]


def test_order_size_capped(engine, edge):
    big = Signal("T", Side.YES, 0.6, 0.45, 44, suggested_contracts=100)
    v = engine.evaluate(big, edge, RiskState())
    assert v.approved and v.contracts == 25


def test_position_limit_shrinks_then_rejects(engine, signal, edge):
    v = engine.evaluate(signal, edge, RiskState(position_contracts=25))
    assert v.approved and v.contracts == 5
    v = engine.evaluate(signal, edge, RiskState(position_contracts=30))
    assert failed(v) == ["position_limit"]


def test_market_exposure_shrinks(engine, signal, edge):
    v = engine.evaluate(signal, edge, RiskState(market_exposure_cents=1900))
    assert v.approved and v.contracts == 2  # 100c room / 44c
    v = engine.evaluate(signal, edge, RiskState(market_exposure_cents=2000))
    assert failed(v) == ["market_exposure"]


def test_total_exposure(engine, signal, edge):
    assert failed(engine.evaluate(signal, edge, RiskState(total_exposure_cents=5000))) == [
        "total_exposure"
    ]


def test_daily_loss(engine, signal, edge):
    assert failed(engine.evaluate(signal, edge, RiskState(daily_pnl_cents=-1000))) == ["daily_loss"]


def test_duplicate_order(engine, signal, edge):
    now = datetime.now(UTC)
    state = RiskState(recent_orders=[("T", now - timedelta(seconds=30))])
    assert failed(engine.evaluate(signal, edge, state, now=now)) == ["duplicate_order"]
    old = RiskState(recent_orders=[("T", now - timedelta(hours=1))])
    assert engine.evaluate(signal, edge, old, now=now).approved
