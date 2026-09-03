# The recorder

The first phase of the strategy in [CRYPTO_15M.md](CRYPTO_15M.md) and the plan. It captures
data and places no orders. Nothing about it can lose money.

## Why it exists

The strategy depends on a quantity no public dataset contains: the settlement index as it
accumulates during the final minute, timestamped next to the order book of the market
settling against it. Kalshi publishes both on one WebSocket. The recorder holds that
connection open and writes everything to disk.

Until this data exists, every backtest of the strategy is guesswork.

## Running it

```bash
kalshi-agent record                     # KXBTC15M and BRTI, into ./captures
kalshi-agent record --series KXBTC15M,KXETH15M --indices BRTI,ETHUSD_RTI
kalshi-agent capture-stats ./captures   # is the data any good?
```

Credentials are required even though this is public market data: Kalshi authenticates the
socket itself at connection time. Run `kalshi-agent setup` first.

## What it captures

| Channel | Why |
| --- | --- |
| `cfbenchmarks_value_5hz`, falling back to `cfbenchmarks_value` | The settlement source. Carries the index, its trailing 60-second average, and in the final minute the windowed average the contract actually settles on. |
| `orderbook_delta` | A snapshot then incremental changes, for every window in its subscription period. |
| `ticker`, `trade` | Top of book and public prints, for cross-checking the reconstructed book. |
| `market_lifecycle_v2` | Window creation, activation and settlement events. |
| `strike_watch` | Our own measurement, described below. |

Files are gzipped JSON lines, rotated hourly, named by the hour they cover. Every record is
`{"t": <nanoseconds>, "ch": <channel>, "m": <the message verbatim>}`. The timestamp is local
and taken the moment the message came off the socket, deliberately separate from any
timestamp inside the message: the difference between the two is the latency measurement the
strategy depends on. The payload is stored unmodified so a decoder bug next year is fixable
by re-reading the capture rather than by collecting the data again.

## The strike measurement

The strike is the index average over the minute ending at the window open, so it cannot
exist before the window starts. Windows are created about a day early carrying a
placeholder, and the real number is stamped at or shortly after the open. Nobody appears to
have published how long that takes, and it determines whether there is an opportunity in
those first seconds at all.

So the recorder polls each window four times a second across its open until the strike
appears, and records every observation. `capture-stats` reports the median and worst delay.
The cost is about a hundred requests per window against a budget of roughly twenty per
second.

## Correctness details that matter

**Prices are integer micro-dollars.** Kalshi moved from whole cents to decimal dollars, and
these markets use a tapered tick: a tenth of a cent below ten cents and above ninety, a full
cent in between. Storing cents as integers would discard exactly the resolution the tails
need, and the tails are where the strategy trades because the fee is smallest there. One
cent is 10,000 micros.

**Quantities are integer hundredths of a contract**, because fractional trading is on and the
minimum order is one hundredth.

**The cost of buying YES is one dollar minus the best NO bid**, not one minus the YES ask.
Getting this backwards invents arbitrage that is not there. It is the most commonly reported
bug in public bots for this exchange.

**A book that has missed a message refuses to answer questions.** Sequence numbers are
checked per subscription. On a gap the book is marked stale and a fresh snapshot is
requested through the existing subscription rather than by reconnecting, which would drop
every other subscription on the connection. A stale book that looks healthy is far more
dangerous than one that admits it is broken.

**The five-per-second index feed is not enabled on every account.** If the subscription is
rejected the recorder falls back to the once-a-second channel and carries on.

## Checking the capture

```bash
kalshi-agent capture-stats ./captures
```

Reports coverage, the index tick rate, how often the feed stalled for more than two seconds,
sequence gaps, how many settlement windows were captured, and the strike delays. What good
looks like: an index rate near one or five per second depending on the channel, no stalls,
few or no sequence gaps, and `settlement_window_ticks` growing by about sixty per quarter
hour.

## Where this goes next

Phase two replays these files offline and scores what the strategy would have said against
what actually settled. That work needs no exchange connection and no capital, and it is
where the strategy lives or dies.
