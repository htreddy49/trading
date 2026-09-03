import httpx
import respx

from kalshi_agent.execution.live import LiveBroker
from kalshi_agent.kalshi.auth import KalshiSigner, generate_test_key
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.kalshi.models import Action, OrderRequest, Side

BASE = "https://demo-api.kalshi.co/trade-api/v2"


async def test_unarmed_broker_never_sends():
    async with KalshiClient(BASE, KalshiSigner("k", generate_test_key())) as client:
        res = await LiveBroker(client).submit(
            OrderRequest(ticker="T", action=Action.BUY, side=Side.YES, count=1, yes_price=50)
        )
    assert res.status == "rejected" and "not armed" in res.message


@respx.mock
async def test_armed_broker_posts_with_client_order_id():
    route = respx.post(f"{BASE}/portfolio/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order": {
                    "order_id": "o1",
                    "ticker": "T",
                    "action": "buy",
                    "side": "yes",
                    "status": "executed",
                    "count": 1,
                    "fill_count": 1,
                }
            },
        )
    )
    async with KalshiClient(BASE, KalshiSigner("k", generate_test_key())) as client:
        res = await LiveBroker(client, armed=True).submit(
            OrderRequest(ticker="T", action=Action.BUY, side=Side.YES, count=1, yes_price=50)
        )
    assert res.status == "filled" and res.order_id == "o1"
    import json

    assert json.loads(route.calls.last.request.content)["client_order_id"].startswith("agent-")
