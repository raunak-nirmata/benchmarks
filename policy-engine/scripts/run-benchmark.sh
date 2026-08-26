#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

require_command kind
require_command kubectl
require_command helm
require_command docker
require_command python3
docker info >/dev/null 2>&1 || die "Docker daemon is unavailable"

run_id=${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
run_dir=${1:-"$REPO_ROOT/results/$run_id"}
if [[ -d "$run_dir" ]] && [[ -n "$(find "$run_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  die "result directory is not empty: $run_dir"
fi
mkdir -p "$run_dir"
{
  printf 'KIND_VERSION=%q\n' "$KIND_VERSION"
  printf 'KUBECTL_VERSION=%q\n' "$KUBECTL_VERSION"
  printf 'HELM_VERSION=%q\n' "$HELM_VERSION"
  printf 'REFERENCE_DOCKER_CLIENT_VERSION=%q\n' "$REFERENCE_DOCKER_CLIENT_VERSION"
  printf 'REFERENCE_DOCKER_SERVER_VERSION=%q\n' "$REFERENCE_DOCKER_SERVER_VERSION"
  printf 'KIND_NODE_IMAGE=%q\n' "$KIND_NODE_IMAGE"
  printf 'KYVERNO_CHART_VERSION=%q\n' "$KYVERNO_CHART_VERSION"
  printf 'KYVERNO_CHART_SHA256=%q\n' "$KYVERNO_CHART_SHA256"
  printf 'GATEKEEPER_CHART_VERSION=%q\n' "$GATEKEEPER_CHART_VERSION"
  printf 'GATEKEEPER_CHART_SHA256=%q\n' "$GATEKEEPER_CHART_SHA256"
  printf 'DURATION_SECONDS=%q\n' "$DURATION_SECONDS"
  printf 'TARGET_RPS=%q\n' "$TARGET_RPS"
  printf 'CONCURRENCY=%q\n' "$CONCURRENCY"
  printf 'REQUEST_TIMEOUT_SECONDS=%q\n' "$REQUEST_TIMEOUT_SECONDS"
  printf 'WARMUP_SECONDS=%q\n' "$WARMUP_SECONDS"
  printf 'WARMUP_RPS=%q\n' "$WARMUP_RPS"
  printf 'IDLE_SAMPLE_SECONDS=%q\n' "$IDLE_SAMPLE_SECONDS"
  printf 'SETTLE_SECONDS=%q\n' "$SETTLE_SECONDS"
  printf 'SAMPLE_INTERVAL_SECONDS=%q\n' "$SAMPLE_INTERVAL_SECONDS"
  printf 'VERIFY_ITERATIONS=%q\n' "$VERIFY_ITERATIONS"
  printf 'REQUIRE_EXCLUSIVE_DOCKER=%q\n' "$REQUIRE_EXCLUSIVE_DOCKER"
  printf 'TRIALS=%q\n' "$TRIALS"
  printf 'ENGINES=%q\n' "$ENGINES"
} > "$run_dir/effective-config.env"

docker ps --no-trunc > "$run_dir/docker-containers-before.txt"
if [[ "$REQUIRE_EXCLUSIVE_DOCKER" == "true" ]] && [[ -n "$(docker ps -q)" ]]; then
  die "running Docker containers would contaminate the shared-host benchmark; see $run_dir/docker-containers-before.txt"
fi

"$REPO_ROOT/scripts/hash-inputs.sh" > "$run_dir/input-checksums.txt"

printf 'trial\tposition\tengine\n' > "$run_dir/trial-order.tsv"

read -r -a engines <<< "$ENGINES"
engine_count=${#engines[@]}
[[ "$engine_count" -eq 4 ]] || die "ENGINES must contain baseline, kyverno, gatekeeper, and native exactly once"
for required_engine in baseline kyverno gatekeeper native; do
  matches=0
  for configured_engine in "${engines[@]}"; do
    [[ "$configured_engine" == "$required_engine" ]] && matches=$((matches + 1))
  done
  [[ "$matches" -eq 1 ]] || die "ENGINES must contain $required_engine exactly once"
done
[[ "$TRIALS" =~ ^[1-9][0-9]*$ ]] || die "TRIALS must be a positive integer"

log "benchmark run $run_id"
log "results: $run_dir"
for ((trial = 1; trial <= TRIALS; trial++)); do
  offset=$(((trial - 1) % engine_count))
  for ((position = 0; position < engine_count; position++)); do
    index=$(((position + offset) % engine_count))
    engine=${engines[$index]}
    printf '%s\t%s\t%s\n' "$trial" "$((position + 1))" "$engine" >> "$run_dir/trial-order.tsv"
    output_dir="$run_dir/trial-${trial}/${engine}"
    "$REPO_ROOT/scripts/run-engine.sh" "$engine" "$trial" "$output_dir"
  done
done

python3 "$REPO_ROOT/scripts/aggregate.py" "$run_dir"
python3 "$REPO_ROOT/scripts/audit-raw-results.py" "$run_dir" \
  --reference "$REPO_ROOT/reference/published-2026-08-19.json" \
  --output "$run_dir/RAW-AUDIT.json"
python3 "$REPO_ROOT/scripts/compare-results.py" \
  "$REPO_ROOT/reference/published-2026-08-19.json" \
  "$run_dir/summary.json" \
  --output "$run_dir/COMPARISON.md"
log "report: $run_dir/REPORT.md"
