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

kubectl kustomize "${DIR}" | sed "s/trading\.example\.com/${FQDN}/g" | kubectl apply -f -

echo "Deployed. Dashboard: https://${FQDN}/"
