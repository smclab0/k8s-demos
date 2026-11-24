#!/usr/bin/env bash
# Fleet bundles only see their own directory, so common/ingress_host.py is a
# copy of ../dmobnk/common/ingress_host.py. Run this after changing that.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
cp ../dmobnk/common/ingress_host.py common/
