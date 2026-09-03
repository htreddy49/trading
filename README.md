# Kalshi Trading Agent

An autonomous trading system for [Kalshi](https://kalshi.com) prediction markets, built
so that **Claude Code is the developer** and the **live-money engine is isolated** from
the development environment.

```
 Claude Code ──► GitHub ──► Codespaces / devcontainer (development)
                     │
                     └── deploy ──► Docker on a cloud VM (trading, 24/7)
```

The agent is not `Claude → Kalshi → trade`. It is a pipeline of small, testable stages
with a risk engine in front of every order:

```
Kalshi ─► Market Collector ─► Market DB ─► Strategy ─► Edge Detector ─► Risk Engine ─► Broker ─► Kalshi
                                                                                        (paper | live)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design,
[docs/CRYPTO_15M.md](docs/CRYPTO_15M.md) for the 15-minute crypto strategy the defaults
target, [docs/RECORDER.md](docs/RECORDER.md) for the data capture, [docs/DEPLOY.md](docs/DEPLOY.md) for running it continuously, and [docs/ROADMAP.md](docs/ROADMAP.md) for the phased plan.

## What is in the box

| Component | Module | Status |
| --- | --- | --- |
| Kalshi REST client with RSA-PSS request signing, retries | `kalshi_agent.kalshi` | done |
| Kalshi WebSocket client: auth, reconnect, sequence-gap detection | `kalshi_agent.kalshi.ws` | done |
| Recorder: captures the settlement index and order books to disk, places no orders | `kalshi_agent.recorder` | done |
| Postgres/SQLite schema + Alembic migrations (markets, snapshots, signals, decisions, orders, fills, positions, P&L, strategy versions, backtests, errors) | `kalshi_agent.db` | done |
| Market collector (polls markets, writes snapshots) | `kalshi_agent.collector` | done |
| Strategy interface + registry + baseline strategies | `kalshi_agent.strategy` | done |
| `averaging_gap`: trades the divergence between the index and the average that settles the contract | `kalshi_agent.strategy.averaging_gap` | done |
| `crypto_15m`: lognormal model against a crypto-exchange price | `kalshi_agent.strategy.crypto` | superseded |
| Data feeds: Kalshi's own settlement index, and Coinbase spot | `kalshi_agent.data` | done |
| Cloud deployment for continuous recording | `deploy/` | done |
| Edge detector (net of Kalshi fees) | `kalshi_agent.signals` | done |
| Risk engine: kill switch, exchange status, edge, liquidity, spread, size, position, exposure, daily loss, duplicate order | `kalshi_agent.risk` | done |
| Paper broker with orderbook-walking fills and virtual P&L | `kalshi_agent.execution.paper` | done |
| Live broker (double-guarded) | `kalshi_agent.execution.live` | done |
| Backtester replaying stored snapshots through the same pipeline; ROI, drawdown, Sharpe, losing streak, fees | `kalshi_agent.backtest` | done |
| Trading engine loop (paper/live share the same code path) | `kalshi_agent.engine` | done |
| FastAPI backend + kill switch endpoint | `kalshi_agent.api` | done |
| Web dashboard with Paper trader / Live trader tabs, served at `/` | `kalshi_agent.api` | done |
| CLI | `kalshi_agent.cli` | done |
| Docker, Compose (postgres, redis, collector, engine, api), devcontainer, CI | repo root | done |
| News/data feeds beyond crypto, performance-analysis agent | | roadmap |

## Quick start (local, paper trading with real market data)

```bash
make install               # uv venv + editable install, copies .env.example -> .env
.venv/bin/kalshi-agent setup       # asks for your API key id and private key, writes .env + secrets/
.venv/bin/kalshi-agent status
.venv/bin/kalshi-agent collect --once      # pull open markets into the DB
.venv/bin/kalshi-agent trade --once        # one paper-trading cycle
.venv/bin/kalshi-agent backtest --days 30  # replay collected snapshots
.venv/bin/kalshi-agent api                 # dashboard at http://localhost:8000/
```

Market data endpoints work without credentials, so `collect` and `backtest` run
immediately; `status` shows your balance once a key is configured.

### With Docker Compose

```bash
cp .env.example .env
docker compose up -d --build      # postgres + redis + migrate + collector + engine + api
docker compose logs -f engine
```

### In GitHub Codespaces

Open the repo in a Codespace; `.devcontainer/devcontainer.json` installs Python 3.11,
Docker-in-Docker, Node 22, the Claude Code extension, and the project itself.

## CLI

| Command | Purpose |
| --- | --- |
| `kalshi-agent setup` | Create the git-ignored `.env` and `secrets/kalshi.pem` from your key id and PEM |
| `kalshi-agent status` | Effective config, Kalshi connectivity, balance, kill-switch state |
| `kalshi-agent db init` | Create tables (dev). Production: `alembic upgrade head` |
| `kalshi-agent record [--series ...] [--indices ...]` | Capture the index feed and order books to disk |
| `kalshi-agent capture-stats <dir>` | Report whether a capture is healthy |
| `kalshi-agent collect [--once] [--orderbooks]` | Polling market collector |
| `kalshi-agent trade [--once]` | Trading engine, paper or live per `TRADING_MODE` |
| `kalshi-agent backtest --days 30 [--strategy x] [--params '{...}']` | Backtest stored snapshots |
| `kalshi-agent api` | Dashboard on `:8000`, API docs at `/docs` |
| `kalshi-agent kill-switch [--release]` | Stop all new orders immediately |
| `kalshi-agent strategies` | List registered strategies |

## Safety rails

* `TRADING_MODE=paper` is the default everywhere, including Compose. Paper mode never sends an order, even against the production exchange.
* `TRADING_MODE=live` with `KALSHI_ENV=prod` refuses to start unless
  `LIVE_TRADING_ACKNOWLEDGED=true`.
* The live broker must be explicitly armed by the engine; the settings validator and the
  broker are two independent guards.
* A kill-switch file (or `POST /kill-switch`) blocks every new order at the first risk check.
* Every signal, risk verdict, order, and fill is persisted so losses can be explained.

## Development

```bash
make check      # ruff + mypy + pytest
make fmt
```

Adding a strategy: subclass `kalshi_agent.strategy.Strategy`, decorate with
`@register`, return a `Signal` with your probability estimate. Sizing and whether to
trade at all remain the risk engine's decision.

## Layout

```
src/kalshi_agent/
  config.py          settings + live-trading guard
  kalshi/            auth, REST client, websocket, API models
  db/                SQLAlchemy models, sessions
  collector/         market polling -> snapshots
  strategy/          Strategy interface, registry, builtin strategies
  signals/           fees, edge detector
  risk/              pre-trade risk engine
  portfolio/         position/P&L accounting
  execution/         paper and live brokers
  backtest/          replay engine + metrics
  engine/            trading loop
  api/               FastAPI app + dashboard.html
  cli.py
alembic/             migrations
docs/                architecture, roadmap, runbook
tests/
```
