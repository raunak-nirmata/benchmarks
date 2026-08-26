#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

cluster_name=${1:?usage: delete-cluster.sh CLUSTER_NAME}
require_command kind
log "deleting cluster $cluster_name"
kind delete cluster --name "$cluster_name"

