#!/usr/bin/env bash
# Deploys DMOBNK (Demo Bank Ldn) to the current kube context, at
# https://dev.smclab.net/dmobnk/, without Fleet.
# Usage: dmobnk/deploy.sh [uk|us|de|sim]   (default uk)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COUNTRY="${1:-uk}"
case "${COUNTRY}" in
  uk) SRC="${DIR}" ;;
  us|de|sim) SRC="${DIR}/countries/${COUNTRY}" ;;
  *) echo "unknown country '${COUNTRY}': use uk, us, de or sim" >&2; exit 1 ;;
esac

kubectl get namespace dmobnk >/dev/null 2>&1 || kubectl create namespace dmobnk
kubectl apply -k "${SRC}"

for d in redis accounts-svc cards-svc loans-svc mortgage-svc rates-svc crossborder-svc web; do
  kubectl -n dmobnk rollout status "deploy/${d}" --timeout=300s
done

# Seed the rates now rather than waiting for the CronJob's first tick.
kubectl -n dmobnk create job --from=cronjob/rates-refresh "rates-refresh-init-$(date +%s)" >/dev/null

echo "Deployed (${COUNTRY}): https://dev.smclab.net/dmobnk/"
