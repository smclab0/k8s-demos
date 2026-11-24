#!/usr/bin/env bash
# Deploys DMOBNK to the current kube context without Fleet. Takes the same
# words as the cluster labels under Fleet (see fleet.yaml), in any order:
#
#   dmobnk/deploy.sh [bank] [branch] [crm] [admin] [uk|us|de|sim]
#
#   (nothing), bank  the online bank, at https://dev.smclab.net/dmobnk/ with
#                    Kube Power at /dmobnk/admin (lenny-new's site)
#   branch           the branch kiosk, at http://branch.<address>.sslip.io/
#   crm              the CRM, at http://crm.<address>.sslip.io/
#   admin            adds Kube Power to whatever's deployed
# Combine them freely (e.g. `bank branch crm admin`); without bank, branch
# or crm you get the bank.
# The country defaults to uk. DMOBNK_SITE=auto deploys the bank on sslip.io
# (targets/) instead of dev.smclab.net, where it then needs `admin` for
# Kube Power.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BANK=""; BRANCH=""; CRM=""; ADMIN=""; COUNTRY=uk
for arg in "$@"; do
  case "${arg}" in
    bank) BANK=1 ;; branch) BRANCH=1 ;; crm) CRM=1 ;; admin) ADMIN=1 ;;
    uk|us|de|sim) COUNTRY="${arg}" ;;
    *) echo "unknown word '${arg}': use bank, branch, crm, admin and uk, us, de or sim" >&2; exit 1 ;;
  esac
done
COMBO="${BANK:+bank}"
[ -n "${BRANCH}" ] && COMBO="${COMBO:+${COMBO}-}branch"
[ -n "${CRM}" ] && COMBO="${COMBO:+${COMBO}-}crm"
COMBO="${COMBO:-bank}${ADMIN:+-admin}"
SRC="${DIR}/targets/${COMBO}/${COUNTRY}"
if [ "${COMBO%-admin}" = bank ] && [ "${DMOBNK_SITE:-dev-smclab}" = dev-smclab ]; then
  # lenny-new's site: dev.smclab.net with TLS on ingress-nginx, always with Kube Power.
  SRC="${DIR}/sites/dev-smclab/${COUNTRY}"
fi

kubectl get namespace dmobnk >/dev/null 2>&1 || kubectl create namespace dmobnk
DEPLOYS="$(kubectl kustomize "${SRC}" | kubectl apply -f - -o name | sed -n 's|^deployment.apps/||p')"
for d in ${DEPLOYS}; do
  kubectl -n dmobnk rollout status "deploy/${d}" --timeout=300s
done

if echo "${DEPLOYS}" | grep -qx web; then
  # Seed the rates now rather than waiting for the CronJob's first tick.
  kubectl -n dmobnk create job --from=cronjob/rates-refresh "rates-refresh-init-$(date +%s)" >/dev/null
fi

# The apps set their Ingress host a few seconds after they start.
url() {
  local host=""
  for _ in $(seq 1 30); do
    host="$(kubectl -n dmobnk get ingress "$1" -o jsonpath='{.spec.rules[0].host}' 2>/dev/null)"
    case "${host}" in *node-ip.sslip.io|"") sleep 2 ;; *) break ;; esac
  done
  local scheme=http; [ "${host}" = dev.smclab.net ] && scheme=https
  echo "${scheme}://${host}$(kubectl -n dmobnk get ingress "$1" -o jsonpath='{.spec.rules[0].http.paths[0].path}')"
}
echo "Deployed ${COMBO} (${COUNTRY}):"
for pair in web:dmobnk branch-web:branch-web crm-web:crm-web kube-power:kube-power; do
  if echo "${DEPLOYS}" | grep -qx "${pair%%:*}"; then echo "  ${pair%%:*}: $(url "${pair#*:}")"; fi
done
if echo "${DEPLOYS}" | grep -qx crm-web; then
  echo "CRM password:        kubectl -n dmobnk get secret dmobnk-crm -o jsonpath='{.data.password}' | base64 -d"
fi
if echo "${DEPLOYS}" | grep -qx kube-power; then
  echo "Kube Power password: kubectl -n dmobnk get secret dmobnk-admin -o jsonpath='{.data.password}' | base64 -d"
fi
