#!/usr/bin/env bash
set -euo pipefail

BENCHMARK_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../config/benchmark.env
source "$BENCHMARK_ROOT/config/benchmark.env"

TOOLS_DIR="$BENCHMARK_ROOT/.tools/bin"
DOWNLOAD_DIR="$BENCHMARK_ROOT/.tools/downloads"
CHART_DIR="$BENCHMARK_ROOT/.tools/charts"
mkdir -p "$TOOLS_DIR" "$DOWNLOAD_DIR" "$CHART_DIR"

for command_name in curl sha256sum tar install docker python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'required command not found: %s\n' "$command_name" >&2
    exit 1
  }
done

case "$(uname -m)" in
  x86_64) TOOL_ARCH=amd64 ;;
  aarch64|arm64) TOOL_ARCH=arm64 ;;
  *) printf 'unsupported architecture: %s\n' "$(uname -m)" >&2; exit 1 ;;
esac

download() {
  local url=$1
  local output=$2
  curl --fail --location --retry 4 --retry-all-errors --silent --show-error \
    "$url" --output "$output"
}

verify_remote_checksum() {
  local file=$1
  local checksum_url=$2
  local checksum_file="$file.checksum"
  download "$checksum_url" "$checksum_file"
  local expected
  expected=$(awk '{print $1}' "$checksum_file")
  printf '%s  %s\n' "$expected" "$file" | sha256sum --check --status
}

verify_pinned_checksum() {
  local file=$1
  local expected=$2
  printf '%s  %s\n' "$expected" "$file" | sha256sum --check --status || {
    printf 'checksum mismatch for %s\n' "$file" >&2
    exit 1
  }
}

if [[ ! -x "$TOOLS_DIR/kind" ]]; then
  kind_asset="$DOWNLOAD_DIR/kind-${KIND_VERSION}-linux-${TOOL_ARCH}"
  download "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${TOOL_ARCH}" "$kind_asset"
  verify_remote_checksum "$kind_asset" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${TOOL_ARCH}.sha256sum"
  install -m 0755 "$kind_asset" "$TOOLS_DIR/kind"
fi

if [[ ! -x "$TOOLS_DIR/kubectl" ]]; then
  kubectl_asset="$DOWNLOAD_DIR/kubectl-${KUBECTL_VERSION}-linux-${TOOL_ARCH}"
  download "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TOOL_ARCH}/kubectl" "$kubectl_asset"
  verify_remote_checksum "$kubectl_asset" "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TOOL_ARCH}/kubectl.sha256"
  install -m 0755 "$kubectl_asset" "$TOOLS_DIR/kubectl"
fi

if [[ ! -x "$TOOLS_DIR/helm" ]]; then
  helm_asset="$DOWNLOAD_DIR/helm-${HELM_VERSION}-linux-${TOOL_ARCH}.tar.gz"
  download "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TOOL_ARCH}.tar.gz" "$helm_asset"
  verify_remote_checksum "$helm_asset" "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TOOL_ARCH}.tar.gz.sha256sum"
  tar -xzf "$helm_asset" -C "$DOWNLOAD_DIR"
  install -m 0755 "$DOWNLOAD_DIR/linux-${TOOL_ARCH}/helm" "$TOOLS_DIR/helm"
fi

export PATH="$TOOLS_DIR:$PATH"

kyverno_chart="$CHART_DIR/kyverno-${KYVERNO_CHART_VERSION}.tgz"
if [[ ! -f "$kyverno_chart" ]]; then
  helm pull kyverno --repo https://kyverno.github.io/kyverno/ \
    --version "$KYVERNO_CHART_VERSION" --destination "$CHART_DIR"
fi
verify_pinned_checksum "$kyverno_chart" "$KYVERNO_CHART_SHA256"

gatekeeper_chart="$CHART_DIR/gatekeeper-${GATEKEEPER_CHART_VERSION}.tgz"
if [[ ! -f "$gatekeeper_chart" ]]; then
  helm pull gatekeeper --repo https://open-policy-agent.github.io/gatekeeper/charts \
    --version "$GATEKEEPER_CHART_VERSION" --destination "$CHART_DIR"
fi
verify_pinned_checksum "$gatekeeper_chart" "$GATEKEEPER_CHART_SHA256"

kind_version=$(kind version | awk '{print $2}')
kubectl_version=$(kubectl version --client=true -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])')
helm_version=$(helm version --template '{{.Version}}')
[[ "$kind_version" == "$KIND_VERSION" ]] || { printf 'kind version mismatch: %s\n' "$kind_version" >&2; exit 1; }
[[ "$kubectl_version" == "$KUBECTL_VERSION" ]] || { printf 'kubectl version mismatch: %s\n' "$kubectl_version" >&2; exit 1; }
[[ "$helm_version" == "$HELM_VERSION" ]] || { printf 'Helm version mismatch: %s\n' "$helm_version" >&2; exit 1; }

printf 'kind:          %s\n' "$kind_version"
printf 'kubectl:       %s\n' "$kubectl_version"
printf 'helm:          %s\n' "$helm_version"
printf 'docker client: %s\n' "$(docker version --format '{{.Client.Version}}')"
printf 'docker server: %s\n' "$(docker version --format '{{.Server.Version}}')"
docker info >/dev/null
