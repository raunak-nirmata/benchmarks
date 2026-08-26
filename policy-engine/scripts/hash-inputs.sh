#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

cd "$REPO_ROOT"
find config policies scripts reference tests -type f \
  ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 \
  | sort -z \
  | xargs -0 sha256sum
sha256sum ".tools/charts/kyverno-${KYVERNO_CHART_VERSION}.tgz"
sha256sum ".tools/charts/gatekeeper-${GATEKEEPER_CHART_VERSION}.tgz"
sha256sum .tools/bin/kind .tools/bin/kubectl .tools/bin/helm
