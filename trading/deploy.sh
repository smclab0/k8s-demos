#!/usr/bin/env bash
# Deploys the trading demo with the Ingress host set to a live sslip.io
# FQDN derived from the cluster's own traefik LoadBalancer IP, resolved
# fresh from the cluster at apply time -- so it's never a stale value
# baked into a file (unlike the static option in overlays/local/).
# Usage: trading/deploy.sh
set -euo pipefail

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

FQDN="trading.${TRAEFIK_IP}.sslip.io"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The dashboard's shared login password lives in a Secret that is never
# committed (this repo is public). Created once from $TRADING_PASSWORD; later
# runs leave it alone. To change it: delete the Secret, re-run with the new
# value, then `kubectl -n apps rollout restart deploy/trading-dashboard`.
if ! kubectl -n apps get secret trading-users >/dev/null 2>&1; then
  if [ -z "${TRADING_PASSWORD:-}" ]; then
    echo "Secret apps/trading-users doesn't exist yet -- re-run as: TRADING_PASSWORD='...' trading/deploy.sh" >&2
    exit 1
  fi
  kubectl get namespace apps >/dev/null 2>&1 || kubectl create namespace apps
  printf '%s' "${TRADING_PASSWORD}" | kubectl -n apps create secret generic trading-users --from-file=password=/dev/stdin
  kubectl -n apps label secret trading-users app.kubernetes.io/part-of=stock-trading-demo
fi

# Version shown on the dashboard: the last commit that touched trading/, so
# commits elsewhere in the repo don't change it (or restart the dashboard).
VERSION=$(git -C "${DIR}" log -1 --format=%h -- . 2>/dev/null || true)
VERSION_DATE=$(git -C "${DIR}" log -1 --format=%cs -- . 2>/dev/null || true)
if [ -n "${VERSION}" ] && [ -n "$(git -C "${DIR}" status --porcelain -- . 2>/dev/null)" ]; then
  VERSION="${VERSION}-dirty"
fi

kubectl kustomize "${DIR}" \
  | sed -e "s/trading\.example\.com/${FQDN}/g" \
        -e "s/value: \"\{0,1\}__APP_VERSION__\"\{0,1\}$/value: \"${VERSION:-unversioned}\"/" \
        -e "s/value: \"\{0,1\}__APP_VERSION_DATE__\"\{0,1\}$/value: \"${VERSION_DATE}\"/" \
  | kubectl apply -f -

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
