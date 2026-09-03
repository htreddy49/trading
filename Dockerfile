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

# Non-root user. A named volume mounted over a path that exists in the image inherits that
# path's ownership, so /data/captures must be created here or the recorder cannot write.
RUN useradd --create-home agent \
    && mkdir -p /app/state /data/captures \
    && chown -R agent:agent /app /data
USER agent
ENV RISK_KILL_SWITCH_FILE=/app/state/KILL_SWITCH \
    CAPTURES_DIR=/data/captures

ENTRYPOINT ["kalshi-agent"]
CMD ["--help"]
