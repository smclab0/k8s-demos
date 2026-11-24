#!/usr/bin/env bash
# Deploys DMOSHP to the current kube context without Fleet. Takes the same
# words as the cluster labels under Fleet (see fleet.yaml), in any order:
#
#   dmoshp/deploy.sh [shop] [sok] [admin]
#
# Without shop or sok you get the shop.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHOP=""; SOK=""; ADMIN=""
for arg in "$@"; do
  case "${arg}" in
    shop) SHOP=1 ;; sok) SOK=1 ;; admin) ADMIN=1 ;;
    *) echo "unknown word '${arg}': use shop, sok and admin" >&2; exit 1 ;;
  esac
done
COMBO="${SHOP:+shop}"
[ -n "${SOK}" ] && COMBO="${COMBO:+${COMBO}-}sok"
COMBO="${COMBO:-shop}${ADMIN:+-admin}"

kubectl get namespace dmoshp >/dev/null 2>&1 || kubectl create namespace dmoshp
DEPLOYS="$(kubectl kustomize "${DIR}/targets/${COMBO}" | kubectl apply -f - -o name | sed -n 's|^deployment.apps/||p')"
for d in ${DEPLOYS}; do
  kubectl -n dmoshp rollout status "deploy/${d}" --timeout=300s
done

# The apps create their Ingress a few seconds after they start.
url() {
  local host=""
  for _ in $(seq 1 30); do
    host="$(kubectl -n dmoshp get ingress "$1" -o jsonpath='{.spec.rules[0].host}' 2>/dev/null)"
    case "${host}" in *node-ip.sslip.io|"") sleep 2 ;; *) break ;; esac
  done
  echo "http://${host}$(kubectl -n dmoshp get ingress "$1" -o jsonpath='{.spec.rules[0].http.paths[0].path}')"
}
echo "Deployed ${COMBO}:"
for d in shop-web sok-web kube-power; do
  if echo "${DEPLOYS}" | grep -qx "${d}"; then echo "  ${d}: $(url "${d}")"; fi
done
if echo "${DEPLOYS}" | grep -qx kube-power; then
  echo "Kube Power password: kubectl -n dmoshp get secret dmoshp-admin -o jsonpath='{.data.password}' | base64 -d   (dmopwd1)"
fi
