#!/usr/bin/env bash
# DMOSHP runs DMOBNK's shared Python modules and Kube Power unchanged. Fleet
# bundles only see their own directory, so they're copies: after changing
# them in ../dmobnk, run this to refresh them here, and commit both.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
cp ../dmobnk/common/ingress_host.py ../dmobnk/common/redis_lite.py ../dmobnk/common/crm_feed.py common/
cp ../dmobnk/admin/base/control.py ../dmobnk/admin/base/kube_power.py admin/base/
echo "copied DMOBNK's shared modules and Kube Power into dmoshp/"
