#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

engine=${1:?usage: create-cluster.sh ENGINE OUTPUT_DIR}
output_dir=${2:?usage: create-cluster.sh ENGINE OUTPUT_DIR}
cluster_name="policy-bench-${engine}"
kubeconfig="$output_dir/kubeconfig"

require_command kind
require_command kubectl
require_command docker
mkdir -p "$output_dir"

while IFS= read -r existing_cluster; do
  if [[ "$existing_cluster" == policy-bench-* ]]; then
    die "benchmark cluster already exists and would contaminate isolation: $existing_cluster"
  fi
done < <(kind get clusters)

log "creating isolated cluster $cluster_name"
kind create cluster \
  --name "$cluster_name" \
  --image "$KIND_NODE_IMAGE" \
  --config "$REPO_ROOT/config/kind.yaml" \
  --kubeconfig "$kubeconfig" \
  --wait 5m

kubectl_for "$kubeconfig" create namespace policy-bench
kubectl_for "$kubeconfig" create namespace policy-bench-control
kubectl_for "$kubeconfig" wait --for=condition=Ready nodes --all --timeout=2m

printf '%s\n' "$cluster_name" > "$output_dir/cluster-name.txt"
kubectl_for "$kubeconfig" version -o json > "$output_dir/kubernetes-version.json"
kubectl_for "$kubeconfig" get nodes -o wide > "$output_dir/nodes.txt"
kubectl_for "$kubeconfig" get namespace kube-system -o json > "$output_dir/cluster-identity.json"
