# Trading Kalshi 15-minute crypto markets

## What these markets are

Kalshi opens a fresh pair of contracts every quarter hour, around the clock, under series
tickers such as `KXBTC15M` and `KXETH15M`. A market resolves YES if the settlement
reference at the window close is at or above `floor_strike`, the level Kalshi fixes when
the window opens.

Settlement uses the CF Benchmarks real-time index for the asset, averaged over the final
60 seconds of the window. It is not the last trade on Kalshi, and the order book cannot
move it. That matters: our edge has to come from a better estimate of where the index
lands, not from reading the Kalshi book.

Individual market tickers look like `KXBTC15M-26SEP031415-T110000`, encoding the window
close in US Eastern time and the strike.

## How the agent trades them

`crypto_15m` (in `strategy/crypto.py`) models the reference price as lognormal with zero
drift over the time remaining:

```
P(YES) = Phi( ln(S / K) / (sigma * sqrt(tau)) )
```

* `S` is spot from Coinbase, polled by the `crypto` data feed with a 5-second cache.
* `K` is `floor_strike` from the Kalshi market object.
* `sigma` is annualised realized volatility of 1-minute Coinbase candles over the last two
  hours, refreshed once a minute and floored at 0.15.
* `tau` is the time to the window close, in years.

The result is then shrunk toward 0.5 by the `shrink` parameter (0.85 by default). That
haircut pays for three things the formula ignores: the basis between Coinbase spot and the
CF Benchmarks index, the 60-second averaging at settlement, and error in the volatility
estimate. Lower it if paper results look over-confident.

The strategy only acts between `min_seconds_left` and `max_seconds_left` before the close.
Too early and the estimate is mostly noise; too late and there is no liquidity to exit
into, and a stale spot quote can be worse than no quote at all. Quotes older than
`max_quote_age_seconds` are ignored outright.

Whatever the model says, the order still has to clear the edge check net of fees and the
full risk chain before anything is sent.

## Fees dominate at these prices

Kalshi's taker fee is `0.07 * C * P * (1 - P)` dollars, rounded up to the cent. That
parabola peaks exactly where these markets trade, near 50 cents, at about 1.75 cents per
contract. Round-tripping a 50-cent contract costs roughly 3.5 cents, which is 7% of the
stake. `RISK_MIN_EDGE` defaults to 0.05 for this reason: a 2-cent theoretical edge is a
loss after fees. Maker fills are about a quarter of the taker rate, so resting orders are
worth exploring once the basics work.

## Configuration

The shipped defaults in `.env.example` already target crypto:

```bash
COLLECTOR_SERIES_TICKERS=["KXBTC15M","KXETH15M"]
COLLECTOR_INTERVAL_SECONDS=10
ENGINE_INTERVAL_SECONDS=15
STRATEGY_NAME=crypto_15m
STRATEGY_PARAMS={"contracts": 5, "shrink": 0.85, "min_seconds_left": 90, "max_seconds_left": 840}
RISK_MIN_EDGE=0.05
RISK_DUPLICATE_WINDOW_SECONDS=120
```

The duplicate-order window has to stay well under 15 minutes, or the agent would only ever
place one order per market lifetime.

## Running it

```bash
kalshi-agent collect --series KXBTC15M,KXETH15M   # snapshots every 10s
kalshi-agent trade                                 # paper cycle every 15s
kalshi-agent api                                   # watch /signals and /decisions
```

Because windows close every 15 minutes, a day of collection is already a few hundred
settled markets, so backtests become meaningful quickly:

```bash
kalshi-agent backtest --days 1 --strategy crypto_15m
```

One caveat on backtesting: snapshots record the Kalshi book, not the Coinbase spot at that
instant. Replaying `crypto_15m` faithfully needs spot history stored alongside each
snapshot. Until that is added, treat backtest output for this strategy as indicative and
trust paper trading, which uses live spot, as the real measurement.

## Before going live

1. Paper trade for several days and compare realized P&L against the model's predicted
   probabilities. If markets you priced at 70% win far less often, the shrink is too small
   or the volatility estimate is off.
2. Check calibration, not just profit. A strategy can be profitable by luck over 50 trades.
3. Only then set `TRADING_MODE=live` and `LIVE_TRADING_ACKNOWLEDGED=true`, and start with
   `RISK_MAX_ORDER_CONTRACTS` at 1.
