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
# The Kube Power control room's admin password. Made once and kept; it's
# never stored in git. Print it with:
#   kubectl -n dmobnk get secret dmobnk-admin -o jsonpath='{.data.password}' | base64 -d
NEW_PASSWORD=""
if ! kubectl -n dmobnk get secret dmobnk-admin >/dev/null 2>&1; then
  NEW_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | cut -c1-20)"
  kubectl -n dmobnk create secret generic dmobnk-admin --from-literal=password="${NEW_PASSWORD}" >/dev/null
fi

kubectl apply -k "${SRC}"

for d in redis accounts-svc cards-svc loans-svc mortgage-svc rates-svc crossborder-svc web; do
  kubectl -n dmobnk rollout status "deploy/${d}" --timeout=300s
done

# Seed the rates now rather than waiting for the CronJob's first tick.
kubectl -n dmobnk create job --from=cronjob/rates-refresh "rates-refresh-init-$(date +%s)" >/dev/null

echo "Deployed (${COUNTRY}): https://dev.smclab.net/dmobnk/"
echo "Control room: https://dev.smclab.net/dmobnk/admin"
if [ -n "${NEW_PASSWORD}" ]; then
  echo "New admin password: ${NEW_PASSWORD}"
fi
