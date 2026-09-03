"""Application settings.

Everything is driven by environment variables (or a local ``.env`` file) so the same
image can run as the market collector, the paper-trading engine, the API, or the live
engine simply by changing configuration.

Two settings are safety-critical and deliberately independent:

* ``KALSHI_ENV``   -- which Kalshi exchange we talk to (``demo`` or ``prod``).
* ``TRADING_MODE`` -- whether orders are simulated (``paper``) or sent (``live``).

Live trading against production additionally requires ``LIVE_TRADING_ACKNOWLEDGED=true``
so that a misconfigured container cannot accidentally trade real money.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KalshiEnv(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


KALSHI_BASE_URLS: dict[KalshiEnv, str] = {
    KalshiEnv.DEMO: "https://demo-api.kalshi.co/trade-api/v2",
    KalshiEnv.PROD: "https://api.elections.kalshi.com/trade-api/v2",
}

KALSHI_WS_URLS: dict[KalshiEnv, str] = {
    KalshiEnv.DEMO: "wss://demo-api.kalshi.co/trade-api/ws/v2",
    KalshiEnv.PROD: "wss://api.elections.kalshi.com/trade-api/ws/v2",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- environment -------------------------------------------------------------
    app_env: str = Field(default="dev", description="dev | test | staging | prod")
    log_level: str = "INFO"
    log_json: bool = False

    # --- Kalshi ------------------------------------------------------------------
    kalshi_env: KalshiEnv = KalshiEnv.DEMO
    kalshi_api_key_id: str | None = Field(default=None, description="Kalshi API key id")
    kalshi_private_key_path: Path | None = Field(
        default=None, description="Path to the RSA private key (PEM) for the API key"
    )
    kalshi_private_key_pem: str | None = Field(
        default=None, description="Inline PEM (alternative to the path; useful in secrets managers)"
    )
    kalshi_timeout_seconds: float = 10.0

    # --- trading mode ------------------------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_acknowledged: bool = False

    # --- persistence -------------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///./kalshi_dev.db",
        description="SQLAlchemy URL. Postgres in docker: postgresql+psycopg://...",
    )
    redis_url: str = "redis://localhost:6379/0"

    # --- collector ---------------------------------------------------------------
    collector_interval_seconds: float = 30.0
    collector_series_tickers: list[str] = Field(
        default_factory=list, description="Only collect these series (empty = all open markets)"
    )
    collector_max_markets: int = 500

    # --- engine ------------------------------------------------------------------
    engine_interval_seconds: float = 60.0
    strategy_name: str = "simple_edge"
    strategy_params: dict[str, float] = Field(default_factory=dict)

    # --- risk limits (all money in cents) ----------------------------------------
    risk_min_edge: float = Field(default=0.04, description="Minimum edge (probability points)")
    risk_max_order_contracts: int = 25
    risk_max_position_contracts: int = 100
    risk_max_market_exposure_cents: int = 5_000
    risk_max_total_exposure_cents: int = 50_000
    risk_max_daily_loss_cents: int = 5_000
    risk_min_liquidity_contracts: int = 50
    risk_max_spread_cents: int = 10
    risk_kill_switch_file: Path = Path("./KILL_SWITCH")

    # --- paper trading -----------------------------------------------------------
    paper_starting_balance_cents: int = 100_000
    paper_fill_slippage_cents: int = 1

    # --- API ---------------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @property
    def kalshi_base_url(self) -> str:
        return KALSHI_BASE_URLS[self.kalshi_env]

    @property
    def kalshi_ws_url(self) -> str:
        return KALSHI_WS_URLS[self.kalshi_env]

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    @property
    def has_kalshi_credentials(self) -> bool:
        return bool(
            self.kalshi_api_key_id and (self.kalshi_private_key_path or self.kalshi_private_key_pem)
        )

    @model_validator(mode="after")
    def _guard_live_trading(self) -> Settings:
        if (
            self.is_live
            and self.kalshi_env is KalshiEnv.PROD
            and not self.live_trading_acknowledged
        ):
            raise ValueError(
                "TRADING_MODE=live with KALSHI_ENV=prod requires LIVE_TRADING_ACKNOWLEDGED=true"
            )
        if self.is_live and not self.has_kalshi_credentials:
            raise ValueError("TRADING_MODE=live requires Kalshi API credentials")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
