# Running continuously on a cloud host

A Codespace sleeps, which interrupts recording. Continuous capture needs a host that stays
up. This is the smallest setup that does that.

## What it runs

| Service | Purpose |
| --- | --- |
| `recorder` | The reason the host exists. Captures the settlement index and order books 24/7. |
| `engine` | Paper trading. Simulated fills, virtual money, no orders reach the exchange. |
| `api` | Dashboard, bound to localhost only. |
| `postgres` | Signals, decisions, orders, P&L. |
| `janitor` | Deletes captures older than `RETENTION_DAYS`. Without it the disk fills. |

## Sizing

Recording produces roughly a gigabyte per day compressed at the message rates measured on
Bitcoin windows, so plan on disk rather than compute. Two virtual CPUs and four gigabytes of
memory are ample; the constraint is storage and network.

Choose a region close to the exchange. Kalshi's matching engine runs in AWS US East (Ohio),
so a host in `us-east-2` cuts most of the round trip. That matters more than anything you
can do in code.

| Provider | A reasonable starting instance |
| --- | --- |
| AWS | `t4g.small` in `us-east-2`, 100 GB gp3 |
| DigitalOcean | Basic 2 vCPU / 4 GB in NYC, 100 GB volume |
| Hetzner | CX22 in Ashburn, 80 GB |

## Setting it up

On a fresh Ubuntu host, as root:

```bash
git clone --branch claude/kalshi-trading-agent-arch-0odpxr \
  https://github.com/htreddy49/trading.git /opt/kalshi-agent
cd /opt/kalshi-agent
./deploy/setup.sh          # installs Docker, then stops and asks for credentials
```

It creates `.env` and stops. Fill in three things:

1. `KALSHI_API_KEY_ID` in `.env`
2. the private key at `/opt/kalshi-agent/secrets/kalshi.pem`, mode `600`
3. leave `TRADING_MODE=paper`

Then run `./deploy/setup.sh` again. It installs a systemd unit, builds the images and starts
everything. The stack comes back by itself after a reboot.

**Use a separate API key for this host.** If it is ever compromised you want to revoke one
key, not the one on your laptop.

## Watching it

```bash
systemctl status kalshi-agent
docker compose -f /opt/kalshi-agent/deploy/docker-compose.yml logs -f recorder
/opt/kalshi-agent/deploy/health.sh
```

`health.sh` checks that every container is running, prints capture statistics, and fails if
the disk is above 85 percent. It is the right thing to run from a cron job or an uptime
monitor.

The dashboard is deliberately bound to localhost rather than exposed. Reach it over an SSH
tunnel from your own machine:

```bash
ssh -N -L 8000:127.0.0.1:8000 user@your-host
```

then open `http://localhost:8000/`. The Recorder tab shows capture health.

## Disk

`RETENTION_DAYS` defaults to fourteen. Lower it if the disk fills, or raise it once you know
how much a day actually costs you:

```bash
echo "RETENTION_DAYS=7" >> /opt/kalshi-agent/.env
systemctl restart kalshi-agent
```

Do not delete captures you have not yet replayed. They are the only record of what the
market did, and they cannot be recreated.

## Updating

```bash
cd /opt/kalshi-agent && git pull && systemctl restart kalshi-agent
```

## Going live, later

Nothing here trades real money. When the strategy has earned it, live trading is a separate
deliberate change on a separate host, requiring `TRADING_MODE=live` and
`LIVE_TRADING_ACKNOWLEDGED=true`, and starting at one contract per order. See
[RUNBOOK.md](RUNBOOK.md).
