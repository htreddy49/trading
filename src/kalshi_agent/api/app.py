"""Dashboard/backend API.

Read-only views over the database (markets, signals, decisions, orders, fills, P&L,
errors) plus the operational controls that must be reachable from outside the engine
container: the kill switch.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import Engine, desc, func, select
from sqlalchemy.orm import Session

from kalshi_agent import __version__
from kalshi_agent.config import Settings, get_settings
from kalshi_agent.db.models import (
    AgentDecision,
    BacktestRun,
    ErrorRow,
    FillRow,
    MarketRow,
    MarketSnapshot,
    OrderRow,
    PnlSnapshot,
    SignalRow,
)
from kalshi_agent.db.session import get_engine, get_session
from kalshi_agent.strategy.registry import list_strategies


def _row(obj: Any) -> dict[str, Any]:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def create_app(engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Kalshi Agent API", version=__version__)

    def db() -> Iterator[Session]:  # dependency
        session = get_session(engine or get_engine())
        try:
            yield session
        finally:
            session.close()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "trading_mode": settings.trading_mode.value,
            "kalshi_env": settings.kalshi_env.value,
            "kill_switch": settings.risk_kill_switch_file.exists(),
        }

    @app.get("/config")
    def config() -> dict[str, Any]:
        data = settings.model_dump(mode="json")
        for key in ("kalshi_api_key_id", "kalshi_private_key_pem", "kalshi_private_key_path"):
            if data.get(key):
                data[key] = "***"
        data["strategies"] = sorted(list_strategies())
        return data

    # -- markets ------------------------------------------------------------------
    @app.get("/markets")
    def markets(
        status: str | None = "open",
        limit: int = Query(100, le=1000),
        session: Session = Depends(db),
    ) -> list[dict[str, Any]]:
        stmt = select(MarketRow).order_by(desc(MarketRow.updated_at)).limit(limit)
        if status:
            stmt = stmt.where(MarketRow.status == status)
        return [_row(m) for m in session.scalars(stmt)]

    @app.get("/markets/{ticker}")
    def market(ticker: str, session: Session = Depends(db)) -> dict[str, Any]:
        m = session.get(MarketRow, ticker)
        if m is None:
            raise HTTPException(404, "unknown market")
        return _row(m)

    @app.get("/markets/{ticker}/snapshots")
    def snapshots(
        ticker: str, limit: int = Query(200, le=5000), session: Session = Depends(db)
    ) -> list[dict[str, Any]]:
        stmt = (
            select(MarketSnapshot)
            .where(MarketSnapshot.ticker == ticker)
            .order_by(desc(MarketSnapshot.ts))
            .limit(limit)
        )
        return [_row(s) for s in reversed(session.scalars(stmt).all())]

    # -- agent --------------------------------------------------------------------
    @app.get("/signals")
    def signals(limit: int = Query(100, le=1000), session: Session = Depends(db)):
        return [
            _row(s)
            for s in session.scalars(select(SignalRow).order_by(desc(SignalRow.ts)).limit(limit))
        ]

    @app.get("/decisions")
    def decisions(limit: int = Query(100, le=1000), session: Session = Depends(db)):
        stmt = select(AgentDecision).order_by(desc(AgentDecision.ts)).limit(limit)
        return [_row(d) for d in session.scalars(stmt)]

    @app.get("/orders")
    def orders(limit: int = Query(100, le=1000), session: Session = Depends(db)):
        return [
            _row(o)
            for o in session.scalars(
                select(OrderRow).order_by(desc(OrderRow.created_at)).limit(limit)
            )
        ]

    @app.get("/fills")
    def fills(limit: int = Query(100, le=1000), session: Session = Depends(db)):
        return [
            _row(f)
            for f in session.scalars(select(FillRow).order_by(desc(FillRow.ts)).limit(limit))
        ]

    @app.get("/pnl")
    def pnl(hours: int = Query(24, le=24 * 90), session: Session = Depends(db)):
        since = datetime.now(UTC) - timedelta(hours=hours)
        stmt = select(PnlSnapshot).where(PnlSnapshot.ts >= since).order_by(PnlSnapshot.ts)
        return [_row(p) for p in session.scalars(stmt)]

    @app.get("/pnl/summary")
    def pnl_summary(session: Session = Depends(db)) -> dict[str, Any]:
        latest = session.scalars(
            select(PnlSnapshot).order_by(desc(PnlSnapshot.ts)).limit(1)
        ).first()
        n_orders = session.scalar(select(func.count()).select_from(OrderRow)) or 0
        n_fills = session.scalar(select(func.count()).select_from(FillRow)) or 0
        return {
            "latest": _row(latest) if latest else None,
            "orders": n_orders,
            "fills": n_fills,
        }

    @app.get("/backtests")
    def backtests(limit: int = Query(50, le=500), session: Session = Depends(db)):
        return [
            _row(b)
            for b in session.scalars(
                select(BacktestRun).order_by(desc(BacktestRun.ts)).limit(limit)
            )
        ]

    @app.get("/errors")
    def errors(limit: int = Query(100, le=1000), session: Session = Depends(db)):
        return [
            _row(e)
            for e in session.scalars(select(ErrorRow).order_by(desc(ErrorRow.ts)).limit(limit))
        ]

    # -- controls -----------------------------------------------------------------
    @app.post("/kill-switch")
    def engage_kill_switch() -> dict[str, Any]:
        settings.risk_kill_switch_file.write_text(datetime.now(UTC).isoformat())
        return {"kill_switch": True}

    @app.delete("/kill-switch")
    def release_kill_switch() -> dict[str, Any]:
        settings.risk_kill_switch_file.unlink(missing_ok=True)
        return {"kill_switch": False}

    return app


app = create_app
