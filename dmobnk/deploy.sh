#!/usr/bin/env bash
# Deploys DMOBNK to the current kube context without Fleet. The first word
# matches the cluster's role label under Fleet (see fleet.yaml):
#
#   dmobnk/deploy.sh [uk|us|de|sim]                the online bank, at
#                                                  https://dev.smclab.net/dmobnk/
#                                                  (with Kube Power at /dmobnk/admin)
#   dmobnk/deploy.sh admin [uk|us|de|sim]          the same; with DMOBNK_SITE=auto,
#                                                  the bank plus Kube Power on sslip.io
#   dmobnk/deploy.sh branch [uk|us|de|sim]         the branch kiosk instead,
#                                                  at http://branch.<address>.sslip.io/
#   dmobnk/deploy.sh branch-admin [uk|us|de|sim]   the kiosk plus Kube Power at /admin
# The country defaults to uk.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROLE=bank
case "${1:-}" in admin|branch|branch-admin) ROLE="$1"; shift ;; esac
COUNTRY="${1:-uk}"
case "${COUNTRY}" in uk|us|de|sim) ;; *) echo "unknown country '${COUNTRY}': use uk, us, de or sim" >&2; exit 1 ;; esac
sub() { if [ "${COUNTRY}" = uk ]; then echo "$1"; else echo "$1/countries/${COUNTRY}"; fi; }

# lenny-new's site: dev.smclab.net with TLS (sites/dev-smclab, which always
# includes Kube Power), instead of the self-configuring sslip.io Ingress
# that Fleet clusters get. Set DMOBNK_SITE=auto to use sslip.io here too.
SITE="${DMOBNK_SITE:-dev-smclab}"
case "${ROLE}" in
  branch)       SRC="$(sub "${DIR}/branch")"; ADMIN="" ;;
  branch-admin) SRC="${DIR}/with-admin/branch/${COUNTRY}"; ADMIN=/admin ;;
  admin)        SRC="${DIR}/with-admin/bank/${COUNTRY}"; ADMIN=/dmobnk/admin ;;
  bank)         SRC="$(sub "${DIR}")"; ADMIN="" ;;
esac
case "${ROLE}:${SITE}" in
  bank:dev-smclab|admin:dev-smclab) SRC="${DIR}/sites/dev-smclab/${COUNTRY}"; ADMIN=/dmobnk/admin ;;
esac

kubectl get namespace dmobnk >/dev/null 2>&1 || kubectl create namespace dmobnk
kubectl apply -k "${SRC}"

if [ "${ROLE#branch}" != "${ROLE}" ]; then
  INGRESS=branch-web; APPS="branch-redis branch-web"; PATH_="/"
else
  INGRESS=dmobnk; APPS="redis accounts-svc cards-svc loans-svc mortgage-svc rates-svc crossborder-svc web"; PATH_="/dmobnk/"
fi
[ -n "${ADMIN}" ] && APPS="${APPS} kube-power"
for d in ${APPS}; do
  kubectl -n dmobnk rollout status "deploy/${d}" --timeout=300s
done

if [ "${INGRESS}" = dmobnk ]; then
  # Seed the rates now rather than waiting for the CronJob's first tick.
  kubectl -n dmobnk create job --from=cronjob/rates-refresh "rates-refresh-init-$(date +%s)" >/dev/null
fi

# The apps set their Ingress host a few seconds after they start.
HOST=""
for _ in $(seq 1 30); do
  HOST="$(kubectl -n dmobnk get ingress "${INGRESS}" -o jsonpath='{.spec.rules[0].host}')"
  case "${HOST}" in *node-ip.sslip.io) sleep 2 ;; *) break ;; esac
done
SCHEME=http; [ "${HOST}" = dev.smclab.net ] && SCHEME=https
echo "Deployed ${ROLE} (${COUNTRY}): ${SCHEME}://${HOST}${PATH_}"
if [ -n "${ADMIN}" ]; then
  # Kube Power made a random password in the dmobnk-admin Secret on its
  # first start (or kept the one already there).
  echo "Kube Power: ${SCHEME}://${HOST}${ADMIN}"
  echo "Its password: kubectl -n dmobnk get secret dmobnk-admin -o jsonpath='{.data.password}' | base64 -d"
fi
