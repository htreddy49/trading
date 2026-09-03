#!/usr/bin/env bash
# Prepare a fresh Ubuntu host to record continuously. Idempotent: safe to re-run.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/htreddy49/trading.git}"
BRANCH="${BRANCH:-claude/kalshi-trading-agent-arch-0odpxr}"
APP_DIR="${APP_DIR:-/opt/kalshi-agent}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Installing Docker"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

say "Fetching the repository into ${APP_DIR}"
if [ -d "${APP_DIR}/.git" ]; then
  git -C "${APP_DIR}" fetch origin "${BRANCH}"
  git -C "${APP_DIR}" checkout "${BRANCH}"
  git -C "${APP_DIR}" reset --hard "origin/${BRANCH}"
else
  git clone --branch "${BRANCH}" "${REPO_URL}" "${APP_DIR}"
fi

cd "${APP_DIR}"
mkdir -p secrets && chmod 700 secrets

if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env — edit it before starting"
  echo "  1. KALSHI_API_KEY_ID=<your key id>"
  echo "  2. put the private key at ${APP_DIR}/secrets/kalshi.pem (chmod 600)"
  echo "  3. keep TRADING_MODE=paper until the strategy is validated"
  echo
  echo "Then run: ${APP_DIR}/deploy/setup.sh"
  exit 0
fi

if ! grep -qE '^KALSHI_API_KEY_ID=.+' .env; then
  echo "KALSHI_API_KEY_ID is not set in ${APP_DIR}/.env" >&2
  exit 1
fi
if ! ls secrets/*.pem >/dev/null 2>&1; then
  echo "No private key found in ${APP_DIR}/secrets/" >&2
  exit 1
fi
chmod 600 secrets/*.pem

say "Installing the systemd unit"
install -m 644 deploy/kalshi-agent.service /etc/systemd/system/kalshi-agent.service
sed -i "s#__APP_DIR__#${APP_DIR}#g" /etc/systemd/system/kalshi-agent.service
systemctl daemon-reload
systemctl enable kalshi-agent

say "Building and starting"
systemctl restart kalshi-agent

say "Done"
cat <<MSG
  Status:   systemctl status kalshi-agent
  Logs:     docker compose -f ${APP_DIR}/deploy/docker-compose.yml logs -f recorder
  Health:   ${APP_DIR}/deploy/health.sh
  Dashboard is bound to localhost. Reach it with an SSH tunnel from your machine:
      ssh -N -L 8000:127.0.0.1:8000 <user>@<this-host>
  then open http://localhost:8000/
MSG
