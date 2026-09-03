# Roadmap

| Phase | Goal | Status |
| --- | --- | --- |
| 1. Infrastructure | GitHub + Claude Code + devcontainer + Docker + Postgres + CI | **done** in this repo |
| 2. Kalshi connection | Signed API client, market collector, snapshots in DB | **done** |
| 3. Paper trading | Strategy → paper orders → virtual P&L, persisted | **done** (baseline strategies) |
| 4. Backtesting | Replay history: wins, losses, ROI, drawdown, Sharpe, losing streak, fees, slippage | **done** (snapshot replay); next: import Kalshi trade history for deeper backfills |
| 5. Risk engine | Edge, liquidity, position/exposure limits, daily loss, duplicate check, kill switch | **done** |
| 6. Live trading | Only after paper + backtests validated | code path exists, gated |

## Next steps

1. **Collect data.** Run the collector for a few weeks against the series you care
   about (`COLLECTOR_SERIES_TICKERS`) so the backtester has history.
2. **Dashboard.** Next.js app over the FastAPI endpoints: markets, positions, orders,
   trades, P&L, strategy, risk, backtests, agent decisions, logs.
3. **More data feeds.** The Coinbase spot/vol feed exists; add news and other sources,
   and persist their features so backtests can replay them. Storing spot alongside each
   snapshot is the prerequisite for backtesting `crypto_15m` faithfully.
4. **Probability models.** Replace the heuristic estimates in `strategy/builtin.py` with
   calibrated models; log calibration (Brier score) per strategy version.
5. **Performance analysis agent.** A Claude Code task that reads `agent_decisions`,
   `fills` and `pnl_snapshots`, explains losing trades, proposes parameter changes,
   runs `kalshi-agent backtest`, and opens a PR. Humans approve; nothing auto-deploys.
6. **Position management.** Exit logic (take-profit / stop / time-based) and settlement
   reconciliation against Kalshi fills for live mode.
7. **WebSocket collector.** Use `kalshi.ws` for low-latency series once a strategy needs it.
8. **Monitoring.** Alerts on `errors`, daily-loss breaches and stale collector data.
