import pytest

from kalshi_agent.config import KalshiEnv, Settings, TradingMode


def test_defaults_are_paper_demo():
    s = Settings(_env_file=None)
    assert s.trading_mode is TradingMode.PAPER
    assert s.kalshi_env is KalshiEnv.DEMO
    assert "demo-api" in s.kalshi_base_url


def test_live_prod_requires_ack():
    with pytest.raises(ValueError, match="LIVE_TRADING_ACKNOWLEDGED"):
        Settings(
            _env_file=None,
            trading_mode="live",
            kalshi_env="prod",
            kalshi_api_key_id="k",
            kalshi_private_key_pem="x",
        )


def test_live_requires_credentials():
    with pytest.raises(ValueError, match="credentials"):
        Settings(_env_file=None, trading_mode="live", kalshi_env="demo")


def test_live_demo_with_credentials_ok():
    s = Settings(
        _env_file=None,
        trading_mode="live",
        kalshi_env="demo",
        kalshi_api_key_id="k",
        kalshi_private_key_pem="x",
    )
    assert s.is_live
