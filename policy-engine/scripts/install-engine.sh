#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

engine=${1:?usage: install-engine.sh ENGINE OUTPUT_DIR}
output_dir=${2:?usage: install-engine.sh ENGINE OUTPUT_DIR}
kubeconfig="$output_dir/kubeconfig"
rendered_dir="$output_dir/rendered"

require_command kubectl
require_command helm
require_command python3

# Policies are rendered per cell, never back into policies/: hash-inputs.sh
# hashes that tree and the run aborts if it changes mid-run. At POLICY_COUNT=1
# the rendered files are byte-identical copies of the templates.
python3 "$REPO_ROOT/scripts/render-policies.py" \
  --engine "$engine" \
  --count "$POLICY_COUNT" \
  --variant "$KYVERNO_CEL_VARIANT" \
  --mode "$POLICY_MODE" \
  --policies-dir "$REPO_ROOT/policies" \
  --output-dir "$rendered_dir"

# Kyverno's policy types report a ready condition; Gatekeeper constraints and
# native admission policies do not, and are gated on the CRD and settle window.
wait_for_policies() {
  local targets="$rendered_dir/wait-targets.tsv" kind name
  [[ -s "$targets" ]] || return 0
  while IFS=$'\t' read -r kind name; do
    [[ -n "$kind" ]] || continue
    if [[ "$kind" == "clusterpolicy" ]]; then
      kubectl_for "$kubeconfig" wait --for=condition=Ready "clusterpolicy/$name" --timeout=3m
    else
      kubectl_for "$kubeconfig" wait \
        --for=jsonpath='{.status.conditionStatus.ready}'=true "$kind/$name" --timeout=3m
    fi
  done < "$targets"
}

# Either the pinned chart archive with its frozen digest, or a local chart
# directory for builds that have no published chart. The local path is a
# deliberate provenance downgrade and says so in the log.
kyverno_chart=""
resolve_kyverno_chart() {
  if [[ -n "$KYVERNO_CHART_PATH" ]]; then
    [[ -d "$KYVERNO_CHART_PATH" ]] \
      || die "KYVERNO_CHART_PATH is not a directory: $KYVERNO_CHART_PATH"
    log "WARNING: using local chart $KYVERNO_CHART_PATH; the pinned-chart gate does not apply"
    kyverno_chart="$KYVERNO_CHART_PATH"
    return
  fi
  kyverno_chart="$REPO_ROOT/.tools/charts/kyverno-${KYVERNO_CHART_VERSION}.tgz"
  [[ -f "$kyverno_chart" ]] || die "missing chart: $kyverno_chart (run scripts/bootstrap-tools.sh)"
  printf '%s  %s\n' "$KYVERNO_CHART_SHA256" "$kyverno_chart" | sha256sum --check --status \
    || die "Kyverno chart checksum mismatch"
}

kyverno_image_args=()
resolve_kyverno_image_args() {
  kyverno_image_args=()
  if [[ -n "$KYVERNO_IMAGE_REGISTRY" ]]; then
    kyverno_image_args+=(
      --set "admissionController.container.image.registry=$KYVERNO_IMAGE_REGISTRY"
      --set "admissionController.initContainer.image.registry=$KYVERNO_IMAGE_REGISTRY"
    )
  fi
  if [[ -n "$KYVERNO_IMAGE_REPOSITORY" ]]; then
    kyverno_image_args+=(--set "admissionController.container.image.repository=$KYVERNO_IMAGE_REPOSITORY")
  fi
  if [[ -n "$KYVERNO_IMAGE_TAG" ]]; then
    kyverno_image_args+=(--set "admissionController.container.image.tag=$KYVERNO_IMAGE_TAG")
  fi
  if [[ -n "$KYVERNO_INIT_IMAGE_REPOSITORY" ]]; then
    kyverno_image_args+=(--set "admissionController.initContainer.image.repository=$KYVERNO_INIT_IMAGE_REPOSITORY")
  fi
  if [[ -n "$KYVERNO_INIT_IMAGE_TAG" ]]; then
    kyverno_image_args+=(--set "admissionController.initContainer.image.tag=$KYVERNO_INIT_IMAGE_TAG")
  fi
  # A kind-loaded image has no registry to pull from.
  if [[ ${#kyverno_image_args[@]} -gt 0 ]]; then
    kyverno_image_args+=(
      --set "admissionController.container.image.pullPolicy=IfNotPresent"
      --set "admissionController.initContainer.image.pullPolicy=IfNotPresent"
    )
    log "WARNING: overriding Kyverno images; this is not a published build"
  fi
}

install_kyverno() {
  local values_file=$1
  resolve_kyverno_chart
  resolve_kyverno_image_args
  KUBECONFIG="$kubeconfig" helm upgrade --install kyverno "$kyverno_chart" \
    --namespace kyverno \
    --create-namespace \
    --values "$values_file" \
    ${kyverno_image_args[@]+"${kyverno_image_args[@]}"} \
    --wait --timeout 10m
  kubectl_for "$kubeconfig" rollout status deployment/kyverno-admission-controller -n kyverno --timeout=3m
  kubectl_for "$kubeconfig" wait --for=jsonpath='{.subsets[0].addresses[0].ip}' endpoints/kyverno-svc -n kyverno --timeout=3m
  retry 30 2 kubectl_for "$kubeconfig" apply -f "$rendered_dir/policies.yaml"
  wait_for_policies
  KUBECONFIG="$kubeconfig" helm get values kyverno --namespace kyverno --all -o yaml > "$output_dir/helm-values-resolved.yaml"
  KUBECONFIG="$kubeconfig" helm get manifest kyverno --namespace kyverno > "$output_dir/helm-manifest.yaml"
}

case "$engine" in
  baseline)
    log "baseline has no policy engine"
    ;;
  kyverno)
    log "installing Kyverno ${KYVERNO_CHART_PATH:-chart $KYVERNO_CHART_VERSION} (ClusterPolicy, policy count $POLICY_COUNT)"
    install_kyverno "$REPO_ROOT/config/kyverno-values.yaml"
    ;;
  kyverno-cel)
    log "installing Kyverno ${KYVERNO_CHART_PATH:-chart $KYVERNO_CHART_VERSION} (CEL policy types, $KYVERNO_CEL_VARIANT variant, policy count $POLICY_COUNT)"
    install_kyverno "$REPO_ROOT/config/kyverno-cel-values.yaml"
    ;;
  gatekeeper)
    log "installing Gatekeeper chart $GATEKEEPER_CHART_VERSION (policy count $POLICY_COUNT)"
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
    # The ConstraintTemplate defines the constraint kind, so it stays single and
    # must be established before the Constraints that use it are applied.
    # POLICY_MODE decides which of these the renderer produced, so apply only
    # what is actually there rather than assuming both halves.
    if [[ -f "$rendered_dir/template.yaml" ]]; then
      retry 30 2 kubectl_for "$kubeconfig" apply -f "$rendered_dir/template.yaml"
      retry 60 2 kubectl_for "$kubeconfig" get crd/policybenchrequireteam.constraints.gatekeeper.sh
      kubectl_for "$kubeconfig" wait --for=condition=Established crd/policybenchrequireteam.constraints.gatekeeper.sh --timeout=3m
      retry 30 2 kubectl_for "$kubeconfig" apply -f "$rendered_dir/constraint.yaml"
    fi
    if [[ -f "$rendered_dir/mutation.yaml" ]]; then
      kubectl_for "$kubeconfig" apply -f "$rendered_dir/mutation.yaml"
    fi
    KUBECONFIG="$kubeconfig" helm get values gatekeeper --namespace gatekeeper-system --all -o yaml > "$output_dir/helm-values-resolved.yaml"
    KUBECONFIG="$kubeconfig" helm get manifest gatekeeper --namespace gatekeeper-system > "$output_dir/helm-manifest.yaml"
    ;;
  native)
    log "installing native Kubernetes admission policies (policy count $POLICY_COUNT)"
    kubectl_for "$kubeconfig" apply -f "$rendered_dir/policies.yaml"
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
