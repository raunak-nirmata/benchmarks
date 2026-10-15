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

# A locally built Kyverno image has to be inside the node before Helm runs: the
# cell creates its own cluster, so there is no other moment to load it. Only the
# Kyverno arms use these, and a missing image fails here rather than as an
# ImagePullBackOff three minutes into a Helm wait.
if [[ "$engine" == "kyverno" || "$engine" == "kyverno-cel" ]]; then
  load_image() {
    local image=$1
    docker image inspect "$image" >/dev/null 2>&1 \
      || die "image not found in the local Docker daemon: $image"
    log "loading $image into $cluster_name"
    kind load docker-image "$image" --name "$cluster_name"
  }
  registry_prefix=""
  [[ -n "$KYVERNO_IMAGE_REGISTRY" ]] && registry_prefix="$KYVERNO_IMAGE_REGISTRY/"
  if [[ -n "$KYVERNO_IMAGE_REPOSITORY" ]] && [[ -n "$KYVERNO_IMAGE_TAG" ]]; then
    load_image "${registry_prefix}${KYVERNO_IMAGE_REPOSITORY}:${KYVERNO_IMAGE_TAG}"
  fi
  if [[ -n "$KYVERNO_INIT_IMAGE_REPOSITORY" ]] && [[ -n "$KYVERNO_INIT_IMAGE_TAG" ]]; then
    load_image "${registry_prefix}${KYVERNO_INIT_IMAGE_REPOSITORY}:${KYVERNO_INIT_IMAGE_TAG}"
  fi
fi

kubectl_for "$kubeconfig" create namespace policy-bench
kubectl_for "$kubeconfig" create namespace policy-bench-control
# The realistic payload names a service account, a config map, and a secret. The
# apiserver's ServiceAccount admission plugin rejects a Pod whose service account
# does not exist, so these have to be present before any request is sent. They are
# created only for that profile, leaving the published profile's cluster state
# untouched.
if [[ "$PAYLOAD_PROFILE" == "realistic" ]]; then
  for namespace in policy-bench policy-bench-control; do
    kubectl_for "$kubeconfig" create serviceaccount policy-bench-app -n "$namespace"
    kubectl_for "$kubeconfig" create configmap policy-bench-config -n "$namespace" \
      --from-literal=app.conf="log_level=info"
    kubectl_for "$kubeconfig" create secret generic policy-bench-secret -n "$namespace" \
      --from-literal=token=placeholder
  done
fi

kubectl_for "$kubeconfig" wait --for=condition=Ready nodes --all --timeout=2m

printf '%s\n' "$cluster_name" > "$output_dir/cluster-name.txt"
kubectl_for "$kubeconfig" version -o json > "$output_dir/kubernetes-version.json"
kubectl_for "$kubeconfig" get nodes -o wide > "$output_dir/nodes.txt"
kubectl_for "$kubeconfig" get namespace kube-system -o json > "$output_dir/cluster-identity.json"
