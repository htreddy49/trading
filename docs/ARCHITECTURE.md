# Architecture

## Principles

1. **Development and live trading are isolated.** Claude Code, Codespaces and the test
   suite never hold production keys. The live engine runs in its own container on its own
   host with its own secrets.
2. **Every order passes the same risk chain**, whether it comes from a backtest, paper
   trading, or live trading. Strategies only produce probability estimates.
3. **Everything is recorded.** Snapshots, signals, risk verdicts, orders, fills, P&L and
   errors go to Postgres so the performance-analysis loop has ground truth.
4. **Humans approve strategy changes.** The research loop can propose and backtest; it
   cannot deploy a new strategy version to the live engine.

## Runtime topology

```
                   ┌──────────────────────── development ───────────────────────┐
  you ──► Claude Code ──► GitHub ──► Codespaces / devcontainer ──► pytest, backtests
                   └────────────────────────────────────────────────────────────┘
                                             │ docker build + deploy
                                             ▼
                   ┌────────────────────────── cloud VM ────────────────────────┐
                   │  postgres   redis                                          │
                   │  collector  ── writes market_snapshots every N seconds     │
                   │  engine     ── strategy → edge → risk → broker             │
                   │  api        ── dashboard backend, kill switch              │
                   └────────────────────────────────────────────────────────────┘
                                             │
                                             ▼
                                          Kalshi
```

## Trading pipeline

```
KALSHI ──► MarketCollector ──► market_snapshots ─┐
                                                 ▼
 Kalshi markets (live) ──► MarketContext(market, orderbook, history, position)
                                                 │
                                                 ▼
                                          Strategy.evaluate  ──► Signal(P_model, side, price, size)
                                                 │
                                                 ▼
                                          EdgeDetector: P_win − price − fees ≥ min_edge ?
                                                 │
                                          no ────┴──── yes
                                          │              ▼
                                        skip       RiskEngine (ordered checks)
                                                     kill switch
                                                     exchange trading active
                                                     edge
                                                     liquidity at limit
                                                     spread
                                                     order size cap
                                                     position limit        (may shrink)
                                                     market exposure cap   (may shrink)
                                                     total exposure cap    (may shrink)
                                                     daily loss limit
                                                     duplicate-order window
                                                         │
                                                         ▼
                                              Broker.submit(OrderRequest)
                                              PaperBroker | LiveBroker
                                                         │
                                                         ▼
                                              orders / fills / positions / pnl tables
```

### Modules

| Stage | Module | Notes |
| --- | --- | --- |
| Market collector | `collector/service.py` | Polls `/markets` (optionally `/orderbook`) and upserts `markets`, appends `market_snapshots`. |
| Market context | `strategy/base.py` | Bundles current market, orderbook, recent snapshots, current position, and `extra` features (news/data feeds plug in here). |
| Strategy | `strategy/` | `Strategy.evaluate(ctx) -> Signal | None`. Registered by name; selected with `STRATEGY_NAME`. |
| Probability model | inside each strategy | `Signal.model_probability` is P(YES). Replace with ML models without touching the rest. |
| Edge detector | `signals/edge.py` | Net edge after Kalshi taker fee `0.07·C·P·(1−P)` rounded up. |
| Risk manager | `risk/engine.py` | Chain above. Returns `RiskVerdict(approved, contracts, checks)`. |
| Position manager | `portfolio/tracker.py` | Cost basis, realized/unrealized P&L, settlement. Shared by paper broker and backtester. |
| Order manager | `execution/` | `PaperBroker` walks the orderbook and charges fees/slippage; `LiveBroker` posts to Kalshi. |

## Research loop (Phase 4+)

```
trade DB ──► Performance analysis ("why did we lose?") ──► Strategy researcher
                                                               │
                                                               ▼
                                                        BacktestEngine
                                                               │
                                                               ▼
                                                  strategy_versions (approved=false)
                                                               │
                                                        human approval
                                                               │
                                                               ▼
                                                    STRATEGY_NAME / params in prod
```

The backtester replays `market_snapshots` through exactly the same
`Strategy → EdgeDetector → RiskEngine → PaperBroker` objects that paper trading uses, so
backtest and paper results are directly comparable.

## Data model

All money is integer cents; probabilities are floats in `[0, 1]`.

| Table | Purpose |
| --- | --- |
| `markets` | Latest known state per market ticker |
| `market_snapshots` | Top-of-book + volume per market per poll |
| `signals` | Every strategy output (model vs market probability, edge, price, size, features) |
| `agent_decisions` | trade / skip / reject with the full list of risk checks |
| `orders`, `fills` | Paper and live orders, tagged by `trading_mode` |
| `positions` | Net position per market per mode |
| `pnl_snapshots` | Cash, exposure, realized, unrealized, fees per engine cycle |
| `strategy_versions` | Named, parameterised, approved-or-not strategy configurations |
| `backtest_runs` | Params + metrics for every backtest |
| `errors` | Component errors for alerting |

## Configuration and safety

* `KALSHI_ENV` (`demo`/`prod`) and `TRADING_MODE` (`paper`/`live`) are independent.
* `TRADING_MODE=live` + `KALSHI_ENV=prod` requires `LIVE_TRADING_ACKNOWLEDGED=true`.
* `LiveBroker` refuses to submit unless armed by the engine.
* Kill switch: file at `RISK_KILL_SWITCH_FILE`, toggled by CLI or `POST/DELETE /kill-switch`.
* Redis is provisioned for live prices, WebSocket fan-out, queues and locks; the current
  polling design does not require it yet.
