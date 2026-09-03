.PHONY: install lint fmt typecheck test check db-init collect trade backtest api up down logs

VENV ?= .venv
BIN  := $(VENV)/bin

install:
	uv venv $(VENV) && uv pip install -e ".[dev]"
	@test -f .env || cp .env.example .env

lint:
	$(BIN)/ruff check .

fmt:
	$(BIN)/ruff format . && $(BIN)/ruff check --fix .

typecheck:
	$(BIN)/mypy

test:
	$(BIN)/pytest -q

check: lint typecheck test

db-init:
	$(BIN)/kalshi-agent db init

migrate:
	$(BIN)/alembic upgrade head

collect:
	$(BIN)/kalshi-agent collect

trade:
	$(BIN)/kalshi-agent trade

backtest:
	$(BIN)/kalshi-agent backtest --days 30

api:
	$(BIN)/kalshi-agent api --reload

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=200
