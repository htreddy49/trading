import httpx
import pytest
import respx

from kalshi_agent.kalshi.auth import KalshiSigner, generate_test_key
from kalshi_agent.kalshi.client import KalshiClient, KalshiError
from kalshi_agent.kalshi.models import Action, OrderRequest, Side

BASE = "https://demo-api.kalshi.co/trade-api/v2"


@pytest.fixture
def signer():
    return KalshiSigner("key-id", generate_test_key())


@respx.mock
async def test_get_markets_paginates():
    respx.get(f"{BASE}/markets").mock(
        side_effect=[
            httpx.Response(
                200, json={"markets": [{"ticker": "A", "status": "open"}], "cursor": "c1"}
            ),
            httpx.Response(
                200, json={"markets": [{"ticker": "B", "status": "open"}], "cursor": ""}
            ),
        ]
    )
    async with KalshiClient(BASE) as client:
        tickers = [m.ticker async for m in client.iter_markets()]
    assert tickers == ["A", "B"]


@respx.mock
async def test_orderbook_parsing():
    respx.get(f"{BASE}/markets/A/orderbook").mock(
        return_value=httpx.Response(200, json={"orderbook": {"yes": [[40, 10]], "no": [[55, 20]]}})
    )
    async with KalshiClient(BASE) as client:
        book = await client.get_orderbook("A")
    assert book.best_yes_ask == 45
    assert book.best_no_ask == 60
    assert book.depth(Side.YES, 45) == 20
    assert book.depth(Side.YES, 44) == 0


@respx.mock
async def test_auth_headers_sent_on_portfolio(signer):
    route = respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json={"balance": 12345, "portfolio_value": 100})
    )
    async with KalshiClient(BASE, signer) as client:
        bal = await client.get_balance()
    assert bal.balance == 12345
    headers = route.calls.last.request.headers
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert "KALSHI-ACCESS-SIGNATURE" in headers


async def test_portfolio_requires_credentials():
    async with KalshiClient(BASE) as client:
        with pytest.raises(KalshiError) as exc:
            await client.get_balance()
    assert exc.value.status_code == 401


@respx.mock
async def test_retries_on_429_then_succeeds(signer, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(KalshiClient, "_backoff", staticmethod(no_sleep))
    route = respx.get(f"{BASE}/exchange/status").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"trading_active": False})]
    )
    async with KalshiClient(BASE, signer) as client:
        status = await client.exchange_status()
    assert status.trading_active is False
    assert route.call_count == 2


@respx.mock
async def test_create_order_body(signer):
    route = respx.post(f"{BASE}/portfolio/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order": {
                    "order_id": "o1",
                    "ticker": "A",
                    "action": "buy",
                    "side": "yes",
                    "status": "resting",
                    "count": 5,
                    "fill_count": 0,
                }
            },
        )
    )
    req = OrderRequest(ticker="A", action=Action.BUY, side=Side.YES, count=5, yes_price=42)
    async with KalshiClient(BASE, signer) as client:
        order = await client.create_order(req)
    assert order.order_id == "o1"
    import json

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "ticker": "A",
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "count": 5,
        "yes_price": 42,
    }


@respx.mock
async def test_api_error_raises():
    respx.get(f"{BASE}/markets/NOPE").mock(
        return_value=httpx.Response(404, json={"message": "market not found"})
    )
    async with KalshiClient(BASE) as client:
        with pytest.raises(KalshiError, match="market not found"):
            await client.get_market("NOPE")
