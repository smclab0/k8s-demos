#!/usr/bin/env bash
# Deploys the trading demo with the Ingress host set to a live sslip.io
# FQDN derived from traefik's current LoadBalancer IP, so it never goes
# stale if that IP changes (unlike the static value baked into a
# gitignored overlay). Usage: trading/deploy-local.sh
set -euo pipefail

TRAEFIK_IP=$(kubectl get svc rke2-traefik -n kube-system \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

if [ -z "${TRAEFIK_IP}" ]; then
  echo "Could not determine traefik's external IP (kubectl get svc rke2-traefik -n kube-system)" >&2
  exit 1
fi

FQDN="trading.${TRAEFIK_IP}.sslip.io"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl kustomize "${DIR}" | sed "s/trading\.example\.com/${FQDN}/g" | kubectl apply -f -

echo "Deployed. Dashboard: https://${FQDN}/"
