# Runbook

## Environments

| Name | `KALSHI_ENV` | `TRADING_MODE` | Where |
| --- | --- | --- | --- |
| dev | demo | paper | laptop / Codespaces, SQLite or Compose |
| staging | demo | live | cloud VM, Compose, demo API key (fake money) |
| prod | prod | live | separate cloud VM, prod API key from a secret manager |

Never point the dev or staging stack at `KALSHI_ENV=prod`.

## Deploying to a cloud VM

```bash
# on the VM
git clone https://github.com/htreddy49/trading.git && cd trading
cp .env.example .env            # edit: KALSHI_ENV, TRADING_MODE, key id, risk limits
mkdir -p secrets && chmod 700 secrets   # PEM at ./secrets/kalshi-demo.pem, mounted read-only into containers
docker compose up -d --build
docker compose logs -f engine
```

Going live on prod additionally requires `LIVE_TRADING_ACKNOWLEDGED=true` in `.env`.

## Stopping trading immediately

```bash
docker compose exec engine kalshi-agent kill-switch     # or: curl -X POST :8000/kill-switch
docker compose stop engine                              # hard stop
```

Release with `kalshi-agent kill-switch --release` or `DELETE /kill-switch`.

## Migrations

```bash
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

The `migrate` service in Compose runs `alembic upgrade head` before the other services start.

## Checking health

* `GET /health` on the API.
* `GET /errors` for recent component errors.
* `GET /pnl/summary` for the latest cash/exposure/P&L snapshot.
* `kalshi-agent status` inside any container.
