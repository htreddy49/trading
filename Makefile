.PHONY: install lint fmt typecheck test check db-init collect trade backtest api up down logs

VENV ?= .venv
BIN  := $(VENV)/bin
UV   := $(shell command -v uv 2>/dev/null)

# Uses uv when available (fast); falls back to the standard library venv + pip.
install:
ifdef UV
	uv venv $(VENV) && VIRTUAL_ENV=$(VENV) uv pip install -e ".[dev]"
else
	python3 -m venv $(VENV) && $(BIN)/pip install -q --upgrade pip && $(BIN)/pip install -q -e ".[dev]"
endif
	@test -f .env || cp .env.example .env
	@echo "installed. next: $(BIN)/kalshi-agent setup"

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
