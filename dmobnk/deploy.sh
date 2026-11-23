#!/usr/bin/env bash
# Deploys DMOBNK (Demo Bank Ldn) to the current kube context, at
# https://dev.smclab.net/dmobnk/. Usage: dmobnk/deploy.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

kubectl get namespace dmobnk >/dev/null 2>&1 || kubectl create namespace dmobnk
kubectl apply -k "${DIR}"

for d in redis accounts-svc cards-svc loans-svc mortgage-svc rates-svc crossborder-svc web; do
  kubectl -n dmobnk rollout status "deploy/${d}" --timeout=300s
done

# Seed the rates now rather than waiting for the CronJob's first tick.
kubectl -n dmobnk create job --from=cronjob/rates-refresh "rates-refresh-init-$(date +%s)" >/dev/null

echo "Deployed: https://dev.smclab.net/dmobnk/"
