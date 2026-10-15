#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

# Scrape the policy engine's own Prometheus endpoint. Client-observed latency
# covers the whole apiserver round trip; this is the webhook's internal view,
# which is where per-policy evaluation cost actually shows up. The request
# counter also proves how many policies really fired, so a policy-count run
# cannot silently measure fewer policies than it installed.
#
# Best effort by design: a missing metrics service must never fail a cell.

engine=${1:?usage: scrape-engine-metrics.sh ENGINE KUBECONFIG OUTPUT_FILE}
kubeconfig=${2:?usage: scrape-engine-metrics.sh ENGINE KUBECONFIG OUTPUT_FILE}
output_file=${3:?usage: scrape-engine-metrics.sh ENGINE KUBECONFIG OUTPUT_FILE}

: > "$output_file"

case "$engine" in
  kyverno|kyverno-cel)
    namespace="kyverno"
    pattern='^(kyverno_|go_memstats_|go_goroutines|process_cpu_seconds_total|process_resident_memory_bytes)'
    ;;
  gatekeeper)
    namespace="gatekeeper-system"
    pattern='^(gatekeeper_|go_memstats_|go_goroutines|process_cpu_seconds_total|process_resident_memory_bytes)'
    ;;
  *)
    log "no engine metrics endpoint for $engine"
    exit 0
    ;;
esac

# Find the metrics service rather than hardcoding its name and port: each chart
# names and numbers it differently, and a wrong guess scrapes nothing while
# still looking like a successful cell.
service_line=$(
  kubectl_for "$kubeconfig" get services --namespace "$namespace" \
    -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.ports[0].port}{"\n"}{end}' 2>/dev/null \
    | grep -- metrics | head -1
) || true
if [[ -z "$service_line" ]]; then
  log "no metrics service in namespace $namespace; continuing"
  exit 0
fi
read -r service port <<< "$service_line"
proxy="/api/v1/namespaces/$namespace/services/$service:$port/proxy/metrics"

if ! kubectl_for "$kubeconfig" get --raw "$proxy" 2>/dev/null | grep -E "$pattern" > "$output_file"; then
  log "engine metrics unavailable for $engine at $service:$port; continuing"
  : > "$output_file"
fi
