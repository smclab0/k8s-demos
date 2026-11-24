#!/usr/bin/env bash
# Deploys the DMOCRP homepage to the current kube context without Fleet.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kubectl get namespace dmocrp >/dev/null 2>&1 || kubectl create namespace dmocrp
kubectl kustomize "${DIR}" | kubectl apply -f -
kubectl -n dmocrp rollout status deploy/dmocrp-web --timeout=300s
for _ in $(seq 1 30); do
  host="$(kubectl -n dmocrp get ingress dmocrp-web -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || true)"
  case "${host}" in *node-ip.sslip.io|"") sleep 2 ;; *) break ;; esac
done
echo "DMOCRP: http://${host}/"
