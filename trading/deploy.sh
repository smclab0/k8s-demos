#!/usr/bin/env bash
# Deploys the trading demo with the Ingress host set to a live wildcard-DNS
# FQDN (trading.<traefik-ip>.sslip.io by default) derived from the cluster's
# own traefik LoadBalancer IP, resolved fresh from the cluster at apply time
# -- so it's never a stale value baked into a file (unlike the static option
# in overlays/local/).
#
# Usage: trading/deploy.sh
# Optional environment variables:
#   WILDCARD_DNS      wildcard-DNS domain for the default host (default: sslip.io,
#                     e.g. nip.io works the same way)
#   TRADING_HOST      full hostname to use instead (e.g. trading.smclab.net);
#                     skips the traefik IP lookup -- its DNS record is up to you
set -euo pipefail

WILDCARD_DNS="${WILDCARD_DNS:-sslip.io}"

if [ -n "${TRADING_HOST:-}" ]; then
  FQDN="${TRADING_HOST}"
else
  TRAEFIK_SVC=$(kubectl get svc -A -l app.kubernetes.io/name=rke2-traefik \
    -o jsonpath='{.items[0].metadata.namespace}{" "}{.items[0].metadata.name}')

  if [ -z "${TRAEFIK_SVC}" ]; then
    echo "Could not find the traefik Service (kubectl get svc -A -l app.kubernetes.io/name=rke2-traefik)" >&2
    exit 1
  fi
  read -r TRAEFIK_NS TRAEFIK_NAME <<< "${TRAEFIK_SVC}"

  TRAEFIK_IP=$(kubectl get svc "${TRAEFIK_NAME}" -n "${TRAEFIK_NS}" \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

  if [ -z "${TRAEFIK_IP}" ]; then
    echo "traefik Service ${TRAEFIK_NS}/${TRAEFIK_NAME} has no LoadBalancer IP yet" >&2
    exit 1
  fi

  FQDN="trading.${TRAEFIK_IP}.${WILDCARD_DNS}"
fi
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl kustomize "${DIR}" | sed "s/trading\.example\.com/${FQDN}/g" | kubectl apply -f -

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

echo "Deployed. Dashboard: https://${FQDN}/"
