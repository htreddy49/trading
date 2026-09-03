"""Async HTTP client for the Kalshi v2 trade API.

Read-only market data endpoints work without credentials. Portfolio and order endpoints
require a :class:`KalshiSigner`. Requests are retried on transient failures and rate
limits with exponential backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

from kalshi_agent.config import Settings, get_settings
from kalshi_agent.kalshi.auth import KalshiSigner
from kalshi_agent.kalshi.models import (
    Balance,
    ExchangeStatus,
    Fill,
    Market,
    Order,
    Orderbook,
    OrderRequest,
    Position,
)
from kalshi_agent.logging import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class KalshiError(Exception):
    def __init__(self, status_code: int, message: str, payload: Any = None) -> None:
        super().__init__(f"Kalshi API error {status_code}: {message}")
        self.status_code = status_code
        self.payload = payload


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "kalshi-agent/0.1"},
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> KalshiClient:
        settings = settings or get_settings()
        signer: KalshiSigner | None = None
        if settings.has_kalshi_credentials:
            assert settings.kalshi_api_key_id
            if settings.kalshi_private_key_pem:
                signer = KalshiSigner.from_pem(
                    settings.kalshi_api_key_id, settings.kalshi_private_key_pem
                )
            else:
                assert settings.kalshi_private_key_path
                signer = KalshiSigner.from_file(
                    settings.kalshi_api_key_id, settings.kalshi_private_key_path
                )
        return cls(settings.kalshi_base_url, signer, timeout=settings.kalshi_timeout_seconds)

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # -- low level ----------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        if auth and self.signer is None:
            raise KalshiError(401, f"{method} {path} requires Kalshi API credentials")

        # Signature path must include the API prefix (/trade-api/v2) but not the query.
        full_path = httpx.URL(self.base_url).path + path
        params = {k: v for k, v in (params or {}).items() if v is not None}

        attempt = 0
        while True:
            headers = self.signer.headers(method, full_path) if self.signer else {}
            try:
                response = await self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise KalshiError(0, f"transport error: {exc}") from exc
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                log.warning("kalshi.retry", status=response.status_code, path=path, attempt=attempt)
                await self._backoff(attempt)
                attempt += 1
                continue

            if response.status_code >= 400:
                try:
                    payload = response.json()
                    message = payload.get("message") or payload.get("error") or response.text
                except ValueError:
                    payload, message = None, response.text
                raise KalshiError(response.status_code, str(message), payload)

            if not response.content:
                return {}
            return response.json()

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(0.5 * 2**attempt, 8.0))

    # -- exchange -------------------------------------------------------------------
    async def exchange_status(self) -> ExchangeStatus:
        return ExchangeStatus.model_validate(await self._request("GET", "/exchange/status"))

    # -- markets ------------------------------------------------------------------
    async def get_markets(
        self,
        *,
        limit: int = 200,
        cursor: str | None = None,
        status: str | None = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        tickers: list[str] | None = None,
    ) -> tuple[list[Market], str | None]:
        payload = await self._request(
            "GET",
            "/markets",
            params={
                "limit": limit,
                "cursor": cursor,
                "status": status,
                "series_ticker": series_ticker,
                "event_ticker": event_ticker,
                "tickers": ",".join(tickers) if tickers else None,
            },
        )
        markets = [Market.model_validate(m) for m in payload.get("markets", [])]
        return markets, payload.get("cursor") or None

    async def iter_markets(
        self, *, max_markets: int | None = None, **kwargs: Any
    ) -> AsyncIterator[Market]:
        cursor: str | None = None
        seen = 0
        while True:
            markets, cursor = await self.get_markets(cursor=cursor, **kwargs)
            for market in markets:
                yield market
                seen += 1
                if max_markets is not None and seen >= max_markets:
                    return
            if not cursor or not markets:
                return

    async def get_market(self, ticker: str) -> Market:
        payload = await self._request("GET", f"/markets/{ticker}")
        return Market.model_validate(payload.get("market", payload))

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        payload = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        return Orderbook.from_api(ticker, payload)

    async def get_market_history(
        self, ticker: str, *, limit: int = 100, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET", f"/markets/{ticker}/history", params={"limit": limit, "cursor": cursor}
        )

    async def get_trades(
        self, *, ticker: str | None = None, limit: int = 100, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "GET", "/markets/trades", params={"ticker": ticker, "limit": limit, "cursor": cursor}
        )

    # -- portfolio ------------------------------------------------------------------
    async def get_balance(self) -> Balance:
        return Balance.model_validate(await self._request("GET", "/portfolio/balance", auth=True))

    async def get_positions(self, *, ticker: str | None = None) -> list[Position]:
        payload = await self._request(
            "GET", "/portfolio/positions", params={"ticker": ticker, "limit": 200}, auth=True
        )
        return [Position.model_validate(p) for p in payload.get("market_positions", [])]

    async def get_orders(
        self, *, ticker: str | None = None, status: str | None = None
    ) -> list[Order]:
        payload = await self._request(
            "GET",
            "/portfolio/orders",
            params={"ticker": ticker, "status": status, "limit": 200},
            auth=True,
        )
        return [Order.model_validate(o) for o in payload.get("orders", [])]

    async def get_fills(self, *, ticker: str | None = None, limit: int = 200) -> list[Fill]:
        payload = await self._request(
            "GET", "/portfolio/fills", params={"ticker": ticker, "limit": limit}, auth=True
        )
        return [Fill.model_validate(f) for f in payload.get("fills", [])]

    async def create_order(self, request: OrderRequest) -> Order:
        payload = await self._request("POST", "/portfolio/orders", json=request.to_api(), auth=True)
        return Order.model_validate(payload.get("order", payload))

    async def cancel_order(self, order_id: str) -> Order:
        payload = await self._request("DELETE", f"/portfolio/orders/{order_id}", auth=True)
        return Order.model_validate(payload.get("order", payload))
