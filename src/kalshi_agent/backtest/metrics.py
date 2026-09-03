"""Performance metrics for a sequence of closed trades and an equity curve."""

from __future__ import annotations

import math
from collections.abc import Sequence


def max_drawdown(equity: Sequence[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            mdd = max(mdd, (peak - value) / peak)
    return mdd


def sharpe_ratio(returns: Sequence[float], periods_per_year: float = 365.0) -> float:
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean / std * math.sqrt(periods_per_year)


def max_losing_streak(pnls: Sequence[float]) -> int:
    best = cur = 0
    for p in pnls:
        cur = cur + 1 if p < 0 else 0
        best = max(best, cur)
    return best


def compute_metrics(
    trade_pnls_cents: Sequence[int],
    equity_curve_cents: Sequence[int],
    fees_cents: int,
    starting_cash_cents: int,
    periods_per_year: float = 365.0,
) -> dict[str, float | int | None]:
    wins = [p for p in trade_pnls_cents if p > 0]
    losses = [p for p in trade_pnls_cents if p < 0]
    total = sum(trade_pnls_cents)
    returns = [
        (equity_curve_cents[i] - equity_curve_cents[i - 1]) / equity_curve_cents[i - 1]
        for i in range(1, len(equity_curve_cents))
        if equity_curve_cents[i - 1] > 0
    ]
    return {
        "trades": len(trade_pnls_cents),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trade_pnls_cents) if trade_pnls_cents else 0.0,
        "net_pnl_cents": total,
        "fees_cents": fees_cents,
        "roi": total / starting_cash_cents if starting_cash_cents else 0.0,
        "avg_win_cents": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_cents": sum(losses) / len(losses) if losses else 0.0,
        # None (JSON null) when there are no losses: infinity is not valid JSON for Postgres.
        "profit_factor": (sum(wins) / -sum(losses)) if losses else None,
        "max_drawdown": max_drawdown(equity_curve_cents),
        "sharpe": sharpe_ratio(returns, periods_per_year),
        "max_losing_streak": max_losing_streak(trade_pnls_cents),
        "final_equity_cents": equity_curve_cents[-1] if equity_curve_cents else starting_cash_cents,
    }
