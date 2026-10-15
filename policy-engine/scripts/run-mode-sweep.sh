#!/usr/bin/env bash
set -euo pipefail
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

# Run the same configuration three times - validating half only, mutating half
# only, then both - and write one comparison across them.
#
# Each mode is a separate benchmark run with its own fresh clusters, because a
# cell installs the policies once at cluster creation. Running them back to back
# from one invocation keeps the host, the build, and the policy count identical,
# which is what makes the three tables comparable; assembling them from runs
# started by hand on different days does not.
#
# Mode order is validate, mutate, both. The two halves run first so that if the
# host is disturbed partway, the halves - the controlled shapes - are the ones
# that survive.

run_dir=${1:?usage: run-mode-sweep.sh RUN_DIR}
if [[ -d "$run_dir" ]] && [[ -n "$(find "$run_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  die "result directory is not empty: $run_dir"
fi
mkdir -p "$run_dir"

modes=${POLICY_MODES_SWEEP:-"validate mutate both"}
log "mode sweep: $modes"
log "results: $run_dir"

for mode in $modes; do
  log "=== mode $mode ==="
  # POLICY_MODE is exported rather than passed, so a preset's ${POLICY_MODE:-...}
  # defers to it and every other knob still comes from the preset.
  POLICY_MODE="$mode" "$REPO_ROOT/scripts/run-benchmark.sh" "$run_dir/$mode"
done

python3 "$REPO_ROOT/scripts/compare-modes.py" "$run_dir"
log "sweep report: $run_dir/MODES.md"
