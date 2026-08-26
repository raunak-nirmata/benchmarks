#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

engine=${1:?usage: install-engine.sh ENGINE OUTPUT_DIR}
output_dir=${2:?usage: install-engine.sh ENGINE OUTPUT_DIR}
kubeconfig="$output_dir/kubeconfig"

require_command kubectl
require_command helm

case "$engine" in
  baseline)
    log "baseline has no policy engine"
    ;;
  kyverno)
    log "installing Kyverno chart $KYVERNO_CHART_VERSION"
    kyverno_chart="$REPO_ROOT/.tools/charts/kyverno-${KYVERNO_CHART_VERSION}.tgz"
    [[ -f "$kyverno_chart" ]] || die "missing chart: $kyverno_chart (run scripts/bootstrap-tools.sh)"
    printf '%s  %s\n' "$KYVERNO_CHART_SHA256" "$kyverno_chart" | sha256sum --check --status \
      || die "Kyverno chart checksum mismatch"
    KUBECONFIG="$kubeconfig" helm upgrade --install kyverno "$kyverno_chart" \
      --namespace kyverno \
      --create-namespace \
      --values "$REPO_ROOT/config/kyverno-values.yaml" \
      --wait --timeout 10m
    kubectl_for "$kubeconfig" rollout status deployment/kyverno-admission-controller -n kyverno --timeout=3m
    kubectl_for "$kubeconfig" wait --for=jsonpath='{.subsets[0].addresses[0].ip}' endpoints/kyverno-svc -n kyverno --timeout=3m
    retry 30 2 kubectl_for "$kubeconfig" apply -f "$REPO_ROOT/policies/kyverno/policies.yaml"
    kubectl_for "$kubeconfig" wait --for=condition=Ready clusterpolicy/policy-bench-mutate --timeout=3m
    kubectl_for "$kubeconfig" wait --for=condition=Ready clusterpolicy/policy-bench-validate --timeout=3m
    KUBECONFIG="$kubeconfig" helm get values kyverno --namespace kyverno --all -o yaml > "$output_dir/helm-values-resolved.yaml"
    KUBECONFIG="$kubeconfig" helm get manifest kyverno --namespace kyverno > "$output_dir/helm-manifest.yaml"
    ;;
  gatekeeper)
    log "installing Gatekeeper chart $GATEKEEPER_CHART_VERSION"
    gatekeeper_chart="$REPO_ROOT/.tools/charts/gatekeeper-${GATEKEEPER_CHART_VERSION}.tgz"
    [[ -f "$gatekeeper_chart" ]] || die "missing chart: $gatekeeper_chart (run scripts/bootstrap-tools.sh)"
    printf '%s  %s\n' "$GATEKEEPER_CHART_SHA256" "$gatekeeper_chart" | sha256sum --check --status \
      || die "Gatekeeper chart checksum mismatch"
    KUBECONFIG="$kubeconfig" helm upgrade --install gatekeeper "$gatekeeper_chart" \
      --namespace gatekeeper-system \
      --create-namespace \
      --values "$REPO_ROOT/config/gatekeeper-values.yaml" \
      --wait --timeout 10m
    kubectl_for "$kubeconfig" rollout status deployment/gatekeeper-controller-manager -n gatekeeper-system --timeout=3m
    kubectl_for "$kubeconfig" wait --for=jsonpath='{.subsets[0].addresses[0].ip}' endpoints/gatekeeper-webhook-service -n gatekeeper-system --timeout=3m
    retry 30 2 kubectl_for "$kubeconfig" apply -f "$REPO_ROOT/policies/gatekeeper/template.yaml"
    retry 60 2 kubectl_for "$kubeconfig" get crd/policybenchrequireteam.constraints.gatekeeper.sh
    kubectl_for "$kubeconfig" wait --for=condition=Established crd/policybenchrequireteam.constraints.gatekeeper.sh --timeout=3m
    kubectl_for "$kubeconfig" apply -f "$REPO_ROOT/policies/gatekeeper/constraint.yaml"
    kubectl_for "$kubeconfig" apply -f "$REPO_ROOT/policies/gatekeeper/mutation.yaml"
    KUBECONFIG="$kubeconfig" helm get values gatekeeper --namespace gatekeeper-system --all -o yaml > "$output_dir/helm-values-resolved.yaml"
    KUBECONFIG="$kubeconfig" helm get manifest gatekeeper --namespace gatekeeper-system > "$output_dir/helm-manifest.yaml"
    ;;
  native)
    log "installing native Kubernetes admission policies"
    kubectl_for "$kubeconfig" apply -f "$REPO_ROOT/policies/native/policies.yaml"
    ;;
  *)
    die "unknown engine: $engine"
    ;;
esac

log "allowing controllers and policy bindings to converge for ${SETTLE_SECONDS}s"
sleep "$SETTLE_SECONDS"
kubectl_for "$kubeconfig" get mutatingwebhookconfigurations,validatingwebhookconfigurations -o yaml > "$output_dir/webhooks.yaml"
kubectl_for "$kubeconfig" get mutatingadmissionpolicies,mutatingadmissionpolicybindings,validatingadmissionpolicies,validatingadmissionpolicybindings -o yaml > "$output_dir/native-policies.yaml"
kubectl_for "$kubeconfig" get deployments,statefulsets,daemonsets --all-namespaces -o yaml > "$output_dir/workloads.yaml"
kubectl_for "$kubeconfig" get pods --all-namespaces -o json > "$output_dir/pods.json"
