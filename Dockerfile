# syntax=docker/dockerfile:1.7
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LOG_JSON=true

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --upgrade pip && pip install .

# Non-root user; the kill-switch file lives in /app/state
RUN useradd --create-home agent && mkdir -p /app/state && chown -R agent:agent /app
USER agent
ENV RISK_KILL_SWITCH_FILE=/app/state/KILL_SWITCH

ENTRYPOINT ["kalshi-agent"]
CMD ["--help"]
