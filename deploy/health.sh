#!/usr/bin/env bash
# One-shot health check. Non-zero exit means something needs attention.
set -uo pipefail
COMPOSE="docker compose -f $(dirname "$0")/docker-compose.yml"
fail=0

echo "== containers =="
$COMPOSE ps --format 'table {{.Service}}\t{{.Status}}'
for svc in recorder engine api postgres; do
  state=$($COMPOSE ps --status running --services 2>/dev/null | grep -Fx "$svc" || true)
  [ -n "$state" ] || { echo "DOWN: $svc"; fail=1; }
done

echo
echo "== capture health =="
if ! $COMPOSE exec -T api kalshi-agent capture-stats /data/captures 2>/dev/null; then
  echo "could not read capture stats"; fail=1
fi

echo
echo "== disk =="
df -h / | tail -1
used=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${used:-0}" -lt 85 ] || { echo "disk above 85% — lower RETENTION_DAYS"; fail=1; }

exit $fail
