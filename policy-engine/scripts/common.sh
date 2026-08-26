#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../config/benchmark.env
source "$REPO_ROOT/config/benchmark.env"
export PATH="$REPO_ROOT/.tools/bin:$PATH"

log() {
  printf '[policy-bench] %s\n' "$*"
}

die() {
  printf '[policy-bench] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1 (run scripts/bootstrap-tools.sh)"
}

retry() {
  local attempts=$1
  local delay=$2
  shift 2
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if "$@"; then
      return 0
    fi
    if ((attempt < attempts)); then
      sleep "$delay"
    fi
  done
  return 1
}

kubectl_for() {
  local kubeconfig=$1
  shift
  KUBECONFIG="$kubeconfig" kubectl "$@"
}
