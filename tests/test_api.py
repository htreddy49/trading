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

    home = client.get("/")
    assert home.status_code == 200 and "Kalshi Agent" in home.text
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


def test_dashboard_endpoint_separates_modes(db, tmp_path):
    from kalshi_agent.db.models import AgentDecision, OrderRow, PnlSnapshot, PositionRow

    settings = Settings(_env_file=None, risk_kill_switch_file=tmp_path / "KS")
    with session_scope(db) as s:
        s.add(
            PnlSnapshot(
                trading_mode="paper",
                cash_cents=9_000,
                exposure_cents=1_000,
                realized_pnl_cents=250,
                unrealized_pnl_cents=-40,
                fees_cents=12,
            )
        )
        s.add(
            PnlSnapshot(
                trading_mode="live",
                cash_cents=500,
                exposure_cents=0,
                realized_pnl_cents=0,
                unrealized_pnl_cents=0,
            )
        )
        s.add(
            PositionRow(
                ticker="KXBTC15M-A", trading_mode="paper", yes_contracts=5, avg_yes_cost=48.0
            )
        )
        s.add(PositionRow(ticker="KXBTC15M-B", trading_mode="paper"))  # closed: filtered out
        s.add(PositionRow(ticker="KXBTC15M-C", trading_mode="live", no_contracts=2))
        s.add(
            OrderRow(
                order_id="o-paper",
                ticker="KXBTC15M-A",
                action="buy",
                side="yes",
                price=48,
                count=5,
                status="filled",
                trading_mode="paper",
            )
        )
        s.add(
            OrderRow(
                order_id="o-live",
                ticker="KXBTC15M-C",
                action="buy",
                side="no",
                price=51,
                count=2,
                status="filled",
                trading_mode="live",
            )
        )
        s.add(
            AgentDecision(ticker="KXBTC15M-A", decision="trade", reason="ok", trading_mode="paper")
        )
        s.add(
            AgentDecision(
                ticker="KXBTC15M-D", decision="reject", reason="edge", trading_mode="paper"
            )
        )
    client = TestClient(create_app(db, settings))

    paper = client.get("/dashboard/paper").json()
    assert paper["active"] is True and paper["mode"] == "paper"
    assert paper["latest"]["cash_cents"] == 9_000
    assert [p["ticker"] for p in paper["positions"]] == ["KXBTC15M-A"]
    assert [o["order_id"] for o in paper["orders"]] == ["o-paper"]
    assert paper["counts"] == {"traded": 1, "rejected": 1, "positions": 1}

    live = client.get("/dashboard/live").json()
    assert live["active"] is False
    assert [o["order_id"] for o in live["orders"]] == ["o-live"]
    assert live["counts"] == {"traded": 0, "rejected": 0, "positions": 1}

    assert client.get("/dashboard/bogus").status_code == 404
    assert len(client.get("/orders").json()) == 2
    assert len(client.get("/orders?mode=live").json()) == 1
    assert client.get("/orders?mode=bogus").status_code == 422
    assert [p["ticker"] for p in client.get("/positions?mode=paper").json()] == ["KXBTC15M-A"]
    assert len(client.get("/positions?mode=paper&open_only=false").json()) == 2
    assert client.get("/pnl/summary?mode=paper").json()["orders"] == 1
