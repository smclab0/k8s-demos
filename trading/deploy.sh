#!/usr/bin/env bash
# Deploys the trading demo. The Ingress host isn't set here: once running, the
# dashboard pod points its own Ingress at trading.<its node's IP>.sslip.io
# (see ingress_host_loop in dashboard.yaml), so this works on whatever cluster
# it's deployed to, and so does a plain `kubectl apply -k trading/`.
#
# Usage: trading/deploy.sh
# Optional environment variables (passed to the dashboard on every run, so
# leaving one out returns it to its default):
#   WILDCARD_DNS      wildcard-DNS domain for the host (default: sslip.io,
#                     e.g. nip.io works the same way)
#   TRADING_HOST      full hostname to use instead (e.g. trading.smclab.net);
#                     its DNS record is up to you
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl apply -k "${DIR}"
# No-op (no restart) when the values haven't changed.
kubectl -n apps set env deploy/trading-dashboard \
  WILDCARD_DNS="${WILDCARD_DNS:-sslip.io}" TRADING_HOST="${TRADING_HOST:-}"

# The CronJob's first scheduled tick can be up to 14 minutes away, leaving the
# dashboard showing "no session recorded yet" until then. If no market_open has
# ever been recorded, start a cycle right now from the CronJob's own template;
# it hands over to the next scheduled tick by itself (see phase_sleep in
# market-open-cron.yaml). Skipped on a redeploy when a session already exists.
if ! kubectl -n apps exec deploy/redis -- redis-cli lrange events 0 19 2>/dev/null | grep -q market_open; then
  kubectl -n apps rollout status deploy/trading-engine --timeout=180s
  kubectl -n apps create job --from=cronjob/market-open "market-open-kickstart-$(date +%s)"
  echo "No session recorded yet -- started the first market cycle now."
fi

# Wait for the dashboard to replace ingress.yaml's placeholder host.
kubectl -n apps rollout status deploy/trading-dashboard --timeout=180s
for _ in $(seq 60); do
  HOST=$(kubectl -n apps get ingress trading-dashboard -o jsonpath='{.spec.rules[0].host}')
  [ "${HOST}" != "trading.node-ip.sslip.io" ] && break
  sleep 2
done
echo "Deployed. Dashboard: https://${HOST}/"
