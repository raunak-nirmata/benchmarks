#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

engine=${1:?usage: run-engine.sh ENGINE TRIAL OUTPUT_DIR}
trial=${2:?usage: run-engine.sh ENGINE TRIAL OUTPUT_DIR}
output_dir=${3:?usage: run-engine.sh ENGINE TRIAL OUTPUT_DIR}
cluster_name="policy-bench-${engine}"
kubeconfig="$output_dir/kubeconfig"
node_name="${cluster_name}-control-plane"

case "$engine" in baseline|kyverno|kyverno-cel|gatekeeper|native) ;; *) die "unknown engine: $engine" ;; esac

require_command python3
require_command kubectl
require_command kind
require_command helm
require_command docker
mkdir -p "$output_dir"

cleanup() {
  if [[ "${KEEP_CLUSTER:-false}" == "true" ]]; then
    log "KEEP_CLUSTER=true; retaining $cluster_name"
    return
  fi
  if kind get clusters 2>/dev/null | grep -Fxq "$cluster_name"; then
    "$REPO_ROOT/scripts/delete-cluster.sh" "$cluster_name" || true
  fi
  rm -f "$kubeconfig"
}
trap cleanup EXIT

log "trial $trial, engine $engine"
"$REPO_ROOT/scripts/create-cluster.sh" "$engine" "$output_dir"
"$REPO_ROOT/scripts/install-engine.sh" "$engine" "$output_dir"

docker ps --no-trunc > "$output_dir/docker-containers-during.txt"
if [[ "$REQUIRE_EXCLUSIVE_DOCKER" == "true" ]]; then
  while IFS= read -r running_name; do
    [[ "$running_name" == "$node_name" ]] \
      || die "unexpected running Docker container during isolated cell: $running_name"
  done < <(docker ps --format '{{.Names}}')
fi

python3 "$REPO_ROOT/scripts/verify-isolation.py" \
  --policy-mode "$POLICY_MODE" \
  --kubeconfig "$kubeconfig" \
  --engine "$engine" \
  --rendered "$output_dir/rendered/rendered.json" \
  --output "$output_dir/isolation.json"

python3 "$REPO_ROOT/scripts/loadgen.py" \
  --kubeconfig "$kubeconfig" \
  --engine "$engine" \
  --timeout "$REQUEST_TIMEOUT_SECONDS" \
  --payload-padding-bytes "$PAYLOAD_PADDING_BYTES" \
  --payload-profile "$PAYLOAD_PROFILE" \
  --policy-mode "$POLICY_MODE" \
  verify \
  --iterations "$VERIFY_ITERATIONS" \
  --output "$output_dir/equivalence.json"

{
  printf 'trial=%s\nengine=%s\n' "$trial" "$engine"
  printf 'captured_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  uname -a
  docker version
  docker info
  lscpu
  free -b
  printf '\nbenchmark tools\n'
  kind version
  kubectl version --client=true -o json
  helm version
} > "$output_dir/host.txt" 2>&1

"$REPO_ROOT/scripts/hash-inputs.sh" > "$output_dir/input-checksums.txt"
cmp "$output_dir/../../input-checksums.txt" "$output_dir/input-checksums.txt" \
  || die "benchmark inputs changed after the run started"
docker inspect "$node_name" > "$output_dir/node-container-inspect.json"
python3 "$REPO_ROOT/scripts/verify-node-runtime.py" \
  --inspect "$output_dir/node-container-inspect.json" \
  --output "$output_dir/node-runtime.json"
docker exec "$node_name" crictl images -o json > "$output_dir/container-images.json"

kubectl_for "$kubeconfig" get --raw /metrics 2>/dev/null \
  | grep -E '^(apiserver_admission_|apiserver_request_duration_seconds|process_|go_memstats_)' \
  > "$output_dir/apiserver-metrics-before.prom" || true
"$REPO_ROOT/scripts/scrape-engine-metrics.sh" "$engine" "$kubeconfig" \
  "$output_dir/engine-metrics-before.prom"

log "warmup: ${WARMUP_SECONDS}s at ${WARMUP_RPS} requests/s"
python3 "$REPO_ROOT/scripts/loadgen.py" \
  --kubeconfig "$kubeconfig" \
  --engine "$engine" \
  --timeout "$REQUEST_TIMEOUT_SECONDS" \
  --payload-padding-bytes "$PAYLOAD_PADDING_BYTES" \
  --payload-profile "$PAYLOAD_PROFILE" \
  --policy-mode "$POLICY_MODE" \
  load \
  --duration "$WARMUP_SECONDS" \
  --rate "$WARMUP_RPS" \
  --concurrency "$CONCURRENCY" \
  --output-dir "$output_dir/warmup" \
  --discard-details

log "idle resource sample: ${IDLE_SAMPLE_SECONDS}s"
python3 "$REPO_ROOT/scripts/sample_resources.py" \
  --node "$node_name" \
  --engine "$engine" \
  --phase idle \
  --duration "$IDLE_SAMPLE_SECONDS" \
  --interval "$SAMPLE_INTERVAL_SECONDS" \
  --output "$output_dir/resources-idle.jsonl"

log "load: ${DURATION_SECONDS}s, ${TARGET_RPS} requests/s, $(python3 -c "print(int(round($DURATION_SECONDS * $TARGET_RPS)))") exact requests"
sample_duration=$(python3 -c "print(float($DURATION_SECONDS) + 1.0)")
printf 'phase\ttimestamp_utc\nload-sampler-start\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" > "$output_dir/phase-boundaries.tsv"
python3 "$REPO_ROOT/scripts/sample_resources.py" \
  --node "$node_name" \
  --engine "$engine" \
  --phase load \
  --duration "$sample_duration" \
  --interval "$SAMPLE_INTERVAL_SECONDS" \
  --output "$output_dir/resources-load.jsonl" &
sampler_pid=$!

set +e
printf 'load-client-start\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" >> "$output_dir/phase-boundaries.tsv"
python3 "$REPO_ROOT/scripts/loadgen.py" \
  --kubeconfig "$kubeconfig" \
  --engine "$engine" \
  --timeout "$REQUEST_TIMEOUT_SECONDS" \
  --payload-padding-bytes "$PAYLOAD_PADDING_BYTES" \
  --payload-profile "$PAYLOAD_PROFILE" \
  --policy-mode "$POLICY_MODE" \
  load \
  --duration "$DURATION_SECONDS" \
  --rate "$TARGET_RPS" \
  --concurrency "$CONCURRENCY" \
  --output-dir "$output_dir"
load_status=$?
printf 'load-client-end\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" >> "$output_dir/phase-boundaries.tsv"
set -e
wait "$sampler_pid"
printf 'load-sampler-end\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" >> "$output_dir/phase-boundaries.tsv"
[[ "$load_status" -eq 0 ]] || die "load generator reported failed or incomplete requests"

python3 "$REPO_ROOT/scripts/loadgen.py" \
  --kubeconfig "$kubeconfig" \
  --engine "$engine" \
  --timeout "$REQUEST_TIMEOUT_SECONDS" \
  --payload-padding-bytes "$PAYLOAD_PADDING_BYTES" \
  --payload-profile "$PAYLOAD_PROFILE" \
  --policy-mode "$POLICY_MODE" \
  verify \
  --iterations "$VERIFY_ITERATIONS" \
  --output "$output_dir/equivalence-after.json"

python3 "$REPO_ROOT/scripts/verify-isolation.py" \
  --policy-mode "$POLICY_MODE" \
  --kubeconfig "$kubeconfig" \
  --engine "$engine" \
  --rendered "$output_dir/rendered/rendered.json" \
  --output "$output_dir/isolation-after.json"

kubectl_for "$kubeconfig" get --raw /metrics 2>/dev/null \
  | grep -E '^(apiserver_admission_|apiserver_request_duration_seconds|process_|go_memstats_)' \
  > "$output_dir/apiserver-metrics-after.prom" || true
"$REPO_ROOT/scripts/scrape-engine-metrics.sh" "$engine" "$kubeconfig" \
  "$output_dir/engine-metrics-after.prom"
kubectl_for "$kubeconfig" get pods --all-namespaces -o wide > "$output_dir/pods-after.txt"
kubectl_for "$kubeconfig" get events --all-namespaces --sort-by=.lastTimestamp > "$output_dir/events-after.txt" || true
docker stats --no-stream --format '{{json .}}' "$node_name" > "$output_dir/node-docker-stats-after.jsonl"

log "trial $trial, engine $engine completed"
