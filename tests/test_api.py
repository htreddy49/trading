from fastapi.testclient import TestClient

from kalshi_agent.api.app import create_app
from kalshi_agent.config import Settings
from kalshi_agent.db.models import MarketRow
from kalshi_agent.db.session import session_scope


def test_api_endpoints(db, tmp_path):
    settings = Settings(
        _env_file=None, risk_kill_switch_file=tmp_path / "KS", kalshi_api_key_id="secret-id"
    )
    with session_scope(db) as s:
        s.add(MarketRow(ticker="M", title="hello"))
    client = TestClient(create_app(db, settings))

    assert client.get("/health").json()["trading_mode"] == "paper"
    assert client.get("/config").json()["kalshi_api_key_id"] == "***"
    assert client.get("/markets").json()[0]["ticker"] == "M"
    assert client.get("/markets/M").json()["title"] == "hello"
    assert client.get("/markets/NOPE").status_code == 404
    for path in ("/signals", "/decisions", "/orders", "/fills", "/pnl", "/backtests", "/errors"):
        assert client.get(path).json() == []
    assert client.get("/pnl/summary").json()["orders"] == 0

    assert client.post("/kill-switch").json()["kill_switch"] is True
    assert client.get("/health").json()["kill_switch"] is True
    assert client.delete("/kill-switch").json()["kill_switch"] is False
