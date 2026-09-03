"""Command line entry point: ``kalshi-agent <command>``."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from kalshi_agent import __version__
from kalshi_agent.config import get_settings
from kalshi_agent.logging import configure_logging, get_logger

app = typer.Typer(help="Kalshi trading agent", no_args_is_help=True)
db_app = typer.Typer(help="Database commands")
app.add_typer(db_app, name="db")

log = get_logger("cli")


@app.callback()
def _init() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)


@app.command()
def version() -> None:
    typer.echo(__version__)


@app.command()
def status() -> None:
    """Show effective configuration and connectivity to Kalshi."""
    from kalshi_agent.kalshi.client import KalshiClient, KalshiError

    s = get_settings()
    typer.echo(f"env={s.kalshi_env.value} mode={s.trading_mode.value} strategy={s.strategy_name}")
    typer.echo(f"database={s.database_url}")
    typer.echo(f"credentials={'yes' if s.has_kalshi_credentials else 'no'}")
    typer.echo(f"kill_switch={'ENGAGED' if s.risk_kill_switch_file.exists() else 'off'}")

    async def check() -> None:
        async with KalshiClient.from_settings(s) as client:
            try:
                st = await client.exchange_status()
                typer.echo(
                    f"exchange_active={st.exchange_active} trading_active={st.trading_active}"
                )
                if s.has_kalshi_credentials:
                    bal = await client.get_balance()
                    typer.echo(f"balance={bal.balance}c portfolio_value={bal.portfolio_value}c")
            except KalshiError as exc:
                typer.echo(f"kalshi error: {exc}")
                if exc.status_code == 401:
                    other = "prod" if s.kalshi_env.value == "demo" else "demo"
                    typer.echo(
                        f"hint: this key id was not accepted by the {s.kalshi_env.value} exchange. "
                        f"Demo (demo.kalshi.co) and production (kalshi.com) keys are separate; "
                        f"if you created the key on the other site set KALSHI_ENV={other} in .env, "
                        "and check KALSHI_API_KEY_ID matches the key whose PEM you saved."
                    )

    asyncio.run(check())


@app.command()
def setup(
    key_id: Annotated[str | None, typer.Option(help="Kalshi API key id")] = None,
    pem_file: Annotated[str | None, typer.Option(help="Path to the downloaded private key")] = None,
) -> None:
    """Create the local, git-ignored credential files: .env and secrets/kalshi.pem."""
    import re
    import shutil
    from pathlib import Path

    env_path, example = Path(".env"), Path(".env.example")
    if not env_path.exists():
        shutil.copy(example, env_path)
        typer.echo("created .env from .env.example")

    key_id = key_id or typer.prompt("Kalshi API key id")
    text = env_path.read_text()
    text, n = re.subn(r"^KALSHI_API_KEY_ID=.*$", f"KALSHI_API_KEY_ID={key_id}", text, flags=re.M)
    if not n:
        text += f"\nKALSHI_API_KEY_ID={key_id}\n"
    env_path.write_text(text)
    typer.echo("wrote KALSHI_API_KEY_ID to .env")

    secrets_dir = Path("secrets")
    secrets_dir.mkdir(exist_ok=True)
    target = secrets_dir / "kalshi.pem"
    if pem_file:
        pem = Path(pem_file).read_text()
    else:
        typer.echo("Paste the private key (including BEGIN/END lines), then press Enter:")
        lines: list[str] = []
        while True:
            line = input()
            lines.append(line)
            if line.startswith("-----END"):
                break
        pem = "\n".join(lines) + "\n"
    if "PRIVATE KEY-----" not in pem:
        typer.secho("that does not look like a PEM private key", fg=typer.colors.RED)
        raise typer.Exit(1)
    target.write_text(pem)
    target.chmod(0o600)
    typer.echo(f"wrote {target}")
    typer.echo("done. next: kalshi-agent status")


@db_app.command("init")
def db_init() -> None:
    """Create all tables (dev). Use ``alembic upgrade head`` in production."""
    from kalshi_agent.db.session import get_engine, init_db

    init_db(get_engine())
    typer.echo("database initialised")


@app.command()
def collect(
    once: Annotated[bool, typer.Option(help="Run a single collection cycle")] = False,
    orderbooks: Annotated[bool, typer.Option(help="Also fetch orderbooks")] = False,
) -> None:
    """Poll Kalshi markets and store snapshots."""
    from kalshi_agent.collector.service import MarketCollector
    from kalshi_agent.db.session import get_engine, init_db
    from kalshi_agent.kalshi.client import KalshiClient

    s = get_settings()
    engine = get_engine()
    init_db(engine)

    async def main() -> None:
        async with KalshiClient.from_settings(s) as client:
            collector = MarketCollector.from_settings(client, engine, s)
            collector.fetch_orderbooks = orderbooks
            if once:
                n = await collector.collect_once()
                typer.echo(f"collected {n} markets")
            else:
                await collector.run_forever(s.collector_interval_seconds)

    asyncio.run(main())


@app.command()
def trade(
    once: Annotated[bool, typer.Option(help="Run a single engine cycle")] = False,
) -> None:
    """Run the trading engine (paper or live depending on TRADING_MODE)."""
    from kalshi_agent.db.session import get_engine, init_db
    from kalshi_agent.engine.loop import TradingEngine

    s = get_settings()
    if s.is_live:
        typer.secho(
            f"LIVE TRADING against {s.kalshi_env.value.upper()} -- real orders will be sent",
            fg=typer.colors.RED,
            bold=True,
        )
    engine = get_engine()
    init_db(engine)

    async def main() -> None:
        te = TradingEngine.from_settings(s, db=engine)
        try:
            if once:
                n = await te.run_once()
                typer.echo(f"processed {n} markets")
            else:
                await te.run_forever(s.engine_interval_seconds)
        finally:
            await te.client.close()

    asyncio.run(main())


@app.command()
def backtest(
    strategy: Annotated[
        str | None, typer.Option(help="Strategy name (default from settings)")
    ] = None,
    days: Annotated[int, typer.Option(help="Look back this many days of snapshots")] = 30,
    tickers: Annotated[str | None, typer.Option(help="Comma separated tickers")] = None,
    params: Annotated[
        str | None, typer.Option(help="JSON strategy params, e.g. '{\"lookback\": 5}'")
    ] = None,
    save: Annotated[bool, typer.Option(help="Persist the run to backtest_runs")] = True,
) -> None:
    """Replay stored snapshots through a strategy and report performance."""
    from kalshi_agent.backtest.engine import BacktestEngine
    from kalshi_agent.db.models import BacktestRun
    from kalshi_agent.db.session import get_engine, init_db, session_scope
    from kalshi_agent.risk.engine import RiskLimits
    from kalshi_agent.strategy.registry import get_strategy

    s = get_settings()
    engine = get_engine()
    init_db(engine)
    name = strategy or s.strategy_name
    strategy_params = json.loads(params) if params else s.strategy_params
    strat = get_strategy(name, **strategy_params)

    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    bars = BacktestEngine.load_bars(
        engine, start=start, end=end, tickers=tickers.split(",") if tickers else None
    )
    if not bars:
        typer.echo("no snapshots in range; run `kalshi-agent collect` first")
        raise typer.Exit(1)

    limits = RiskLimits.from_settings(s)
    bt = BacktestEngine(
        strat,
        limits=limits,
        starting_cash_cents=s.paper_starting_balance_cents,
        min_edge=s.risk_min_edge,
        slippage_cents=s.paper_fill_slippage_cents,
    )
    result = asyncio.run(bt.run(bars))
    typer.echo(json.dumps(result.metrics, indent=2, default=str))
    if save:
        with session_scope(engine) as session:
            session.add(
                BacktestRun(
                    strategy=name,
                    params=strategy_params,
                    start=start,
                    end=end,
                    metrics=result.metrics,
                )
            )


@app.command()
def api(
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
) -> None:
    """Serve the dashboard API."""
    import uvicorn

    from kalshi_agent.db.session import get_engine, init_db

    s = get_settings()
    init_db(get_engine())
    uvicorn.run(
        "kalshi_agent.api.app:app",
        factory=True,
        host=host or s.api_host,
        port=port or s.api_port,
        reload=reload,
    )


@app.command("kill-switch")
def kill_switch(
    release: Annotated[bool, typer.Option(help="Release instead of engage")] = False,
) -> None:
    """Engage (default) or release the kill switch. Engaged = no new orders."""
    s = get_settings()
    if release:
        s.risk_kill_switch_file.unlink(missing_ok=True)
        typer.echo("kill switch released")
    else:
        s.risk_kill_switch_file.write_text(datetime.now(UTC).isoformat())
        typer.secho("kill switch ENGAGED", fg=typer.colors.RED, bold=True)


@app.command()
def strategies() -> None:
    """List registered strategies."""
    from kalshi_agent.strategy.registry import list_strategies

    for name, cls in sorted(list_strategies().items()):
        typer.echo(f"{name}\tv{cls.version}\t{(cls.__doc__ or '').strip().splitlines()[0]}")


if __name__ == "__main__":
    app()
