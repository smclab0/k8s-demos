#!/usr/bin/env bash
# Deploys DMOBNK to the current kube context without Fleet.
#
#   dmobnk/deploy.sh [uk|us|de|sim]          the online bank, at
#                                            https://dev.smclab.net/dmobnk/
#   dmobnk/deploy.sh branch [uk|us|de|sim]   the branch kiosk instead,
#                                            at http://branch.<node IP>.sslip.io/
# The country defaults to uk.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=bank
if [ "${1:-}" = "branch" ]; then APP=branch; shift; fi
COUNTRY="${1:-uk}"
ROOT="${DIR}"
[ "${APP}" = branch ] && ROOT="${DIR}/branch"
case "${COUNTRY}" in
  uk) SRC="${ROOT}" ;;
  us|de|sim) SRC="${ROOT}/countries/${COUNTRY}" ;;
  *) echo "unknown country '${COUNTRY}': use uk, us, de or sim" >&2; exit 1 ;;
esac

kubectl get namespace dmobnk >/dev/null 2>&1 || kubectl create namespace dmobnk

if [ "${APP}" = branch ]; then
  kubectl apply -k "${SRC}"
  for d in branch-redis branch-web; do
    kubectl -n dmobnk rollout status "deploy/${d}" --timeout=300s
  done
  # branch-web sets the Ingress host a few seconds after it starts.
  HOST=""
  for _ in $(seq 1 30); do
    HOST="$(kubectl -n dmobnk get ingress branch-web -o jsonpath='{.spec.rules[0].host}')"
    [ "${HOST}" != "branch.node-ip.sslip.io" ] && break
    sleep 2
  done
  echo "Deployed the branch (${COUNTRY}): http://${HOST}/"
  exit 0
fi

# The Kube Power control room's admin password. Made once and kept; it's
# never stored in git. Print it with:
#   kubectl -n dmobnk get secret dmobnk-admin -o jsonpath='{.data.password}' | base64 -d
NEW_PASSWORD=""
if ! kubectl -n dmobnk get secret dmobnk-admin >/dev/null 2>&1; then
  NEW_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | cut -c1-20)"
  kubectl -n dmobnk create secret generic dmobnk-admin --from-literal=password="${NEW_PASSWORD}" >/dev/null
fi

# lenny-new's site: dev.smclab.net with TLS (sites/dev-smclab), instead of
# the self-configuring sslip.io Ingress that Fleet clusters get. Set
# DMOBNK_SITE=auto to deploy the sslip.io one here too.
if [ "${DMOBNK_SITE:-dev-smclab}" = "dev-smclab" ]; then
  SRC="${DIR}/sites/dev-smclab/${COUNTRY}"
fi
kubectl apply -k "${SRC}"

for d in redis accounts-svc cards-svc loans-svc mortgage-svc rates-svc crossborder-svc web; do
  kubectl -n dmobnk rollout status "deploy/${d}" --timeout=300s
done

# Seed the rates now rather than waiting for the CronJob's first tick.
kubectl -n dmobnk create job --from=cronjob/rates-refresh "rates-refresh-init-$(date +%s)" >/dev/null

if [ "${DMOBNK_SITE:-dev-smclab}" = "dev-smclab" ]; then URL="https://dev.smclab.net/dmobnk/"; else
  sleep 15; URL="http://$(kubectl -n dmobnk get ingress dmobnk -o jsonpath='{.spec.rules[0].host}')/dmobnk/"; fi
echo "Deployed (${COUNTRY}): ${URL}"
echo "Control room: ${URL}admin"
if [ -n "${NEW_PASSWORD}" ]; then
  echo "New admin password: ${NEW_PASSWORD}"
fi
