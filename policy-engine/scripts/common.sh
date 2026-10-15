#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# A preset names one run shape - engines, policy count, variant, payload, rate -
# so a result can be reproduced from a name rather than from a shell invocation.
# It is sourced before benchmark.env, whose every value is a ${VAR:-default}, so
# the preset overrides the published default and the environment overrides the
# preset.
PRESET=${PRESET:-}
if [[ -n "$PRESET" ]]; then
  preset_file="$REPO_ROOT/config/presets/$PRESET.env"
  if [[ ! -f "$preset_file" ]]; then
    printf '[policy-bench] ERROR: unknown preset: %s\n' "$PRESET" >&2
    printf '[policy-bench] available: %s\n' \
      "$(cd "$REPO_ROOT/config/presets" && ls -- *.env 2>/dev/null | sed 's/\.env$//' | tr '\n' ' ')" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$preset_file"
fi
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
