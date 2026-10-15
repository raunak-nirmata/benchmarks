# Kubernetes admission policy benchmark

This directory is the shareable reproduction package for the benchmark in [Comparing Policy Engines in Kubernetes](https://merdincz.com/en/comparing-kubernetes-policy-engines/). It freezes the exact 60-second profile, policy implementations, chart versions, and aggregation rules used for the published numbers.

The retained historical evidence has been independently re-read from every raw CSV and resource JSONL file. All published values recompute exactly after display rounding: 12 fresh-cluster cells, 144,000 measured requests, 180/180 recorded semantic cases per cell, and no failed measured request. The machine-readable result of that audit is [`reference/published-2026-08-19-audit.json`](reference/published-2026-08-19-audit.json).

This is a reproducible pilot microbenchmark, not a universal policy-engine ranking. Read [`LIMITATIONS.md`](LIMITATIONS.md) before quoting the results.

## Exact published profile

| Item | Value |
|---|---|
| Kubernetes | 1.36.1, digest-pinned kind node image |
| kind | 0.32.0 |
| Kyverno | chart 3.8.2, app 1.18.2 |
| Gatekeeper | chart 3.23.0 |
| Trials | 3 fresh clusters per configuration |
| Configurations | baseline, Kyverno, Gatekeeper, native |
| Warm-up | 15 seconds at 50 requests/s |
| Measurement | 60 seconds at 200 requests/s |
| Requests | exactly 12,000 per cell |
| Maximum client workers | 128 |
| Idle resource sample | 15 seconds at 1 Hz |
| Policy scenario | validation and mutation together |
| Measured object | matched, valid, dry-run Pod `CREATE` |

The profile is defined in [`config/benchmark.env`](config/benchmark.env). The Kyverno and Gatekeeper chart archives are checked against frozen SHA-256 digests before installation.

## Run it

Prerequisites:

- Linux on a dedicated or otherwise quiet host;
- Docker with permission to use its daemon;
- Python 3.10 or newer, `curl`, `tar`, and `sha256sum`;
- outbound access for the first tool/chart download and container-image pulls;
- at least four available CPU cores, about 8 GiB RAM, and 10 GiB free Docker storage.

From this directory:

```bash
./scripts/bootstrap-tools.sh
./scripts/run-benchmark.sh results/reproduction-$(date -u +%Y%m%dT%H%M%SZ)
```

For a run shape other than the published one - more policies per arm, a
different payload, an engine A/B - use a named preset rather than setting the
knobs by hand:

```bash
PRESET=five-arm-batched-n16 ./scripts/run-benchmark.sh results/batched-n16
```

To measure the validating and mutating halves separately as well as together,
in one invocation:

```bash
PRESET=five-arm-validate-n16 ./scripts/run-mode-sweep.sh results/sweep
```

See [`config/presets/README.md`](config/presets/README.md) for the list and
[`SCALING.md`](SCALING.md) for what each knob does.

The exact suite takes about 33 minutes on the original four-core host. It creates and deletes 12 kind clusters sequentially. Do not run other CPU-, memory-, disk-, or Docker-intensive workloads at the same time.

The run produces:

- `REPORT.md` and `summary.json` with recomputed results;
- `COMPARISON.md` and `COMPARISON.json` against the published reference;
- every raw request row and one-second resource sample;
- pre-load and post-load semantic/isolation results;
- exact trial order, effective configuration, and input checksums;
- host, Docker quota, Kubernetes, webhook, resolved Helm, workload, and image inventories.

Absolute latency is host-sensitive. A valid reproduction means the same profile and semantics were used and the complete per-trial distributions are reported; it does not mean every percentile must be numerically identical.

On a dedicated Docker host, use `REQUIRE_EXCLUSIVE_DOCKER=true` to abort if any container other than the active kind node is running. This is a stricter provenance gate than the historical run and does not change the admission workload.

## Audit the published raw data

If the original `results/benchmark-20260819-exact` directory is available next to this package, run:

```bash
python3 scripts/audit-raw-results.py \
  ../results/benchmark-20260819-exact \
  --reference reference/published-2026-08-19.json \
  --output /tmp/published-audit.json \
  --verify-published-values \
  --verify-frozen-artifact-hashes
```

This audit is independent of the original aggregator. It verifies request counts and indices, the constant-rate schedule, status and mutation results, latency accounting, percentile calculations, semantic gates, isolation gates, sample counts, resource calculations, configuration consistency, and the published `summary.json`/`REPORT.md` hashes.

To compare any already aggregated run:

```bash
python3 scripts/compare-results.py \
  reference/published-2026-08-19.json \
  results/YOUR_RUN/summary.json \
  --output results/YOUR_RUN/COMPARISON.md
```

## Policy equivalence

Kyverno YAML, Gatekeeper Rego/mutation resources, and native CEL cannot be textually identical. They implement the same observable contract: matching namespace, resource, operations, label, add-only annotation mutation, validation decision, and rejection message. [`policies/CONTRACT.md`](policies/CONTRACT.md) is the truth table enforced before and after load.

The performance workload exercises `CREATE` only. `UPDATE`, invalid, unmatched, and no-overwrite cases are correctness probes.

## Fairness details that must remain visible

- Every cell starts with a new single-control-plane cluster and installs at most one policy engine.
- Kyverno and Gatekeeper use one admission replica and no configured container CPU/memory requests or limits. Native runs inside `kube-apiserver`; it has no comparable policy-only container.
- Gatekeeper audit is disabled. Kyverno background, cleanup, report controllers, and report generation are disabled.
- The exact published Gatekeeper chart configuration retained generate-operation support and fail-open webhook defaults. Kyverno chart 3.8.2 also retained its default `--generateValidatingAdmissionPolicy=true`, although the captured Kyverno cells contained no generated native policies. Gatekeeper used `Ignore` with one-/three-second timeouts; Kyverno used `Fail`/10 seconds and native used `Fail`. No measured request failed, but these failure semantics were not equal.
- Warm-up and measurement use separate client processes. The historical client metric includes small request-preparation costs.
- Whole-node usage is the primary resource comparison. Policy-only native resource usage cannot be separated from other API-server work.

More detail is in [`METHODOLOGY.md`](METHODOLOGY.md) and [`LIMITATIONS.md`](LIMITATIONS.md).

## Policy-count scaling and engine A/B runs

The published profile installs one mutate and one validate policy per arm, which cannot show
costs that grow with the number of policies matching one object, and cannot compare two builds
of the same engine. `POLICY_COUNT` and `PROFILE` cover both, and any run that uses them is
marked as not an article reproduction. See [`SCALING.md`](SCALING.md).

## Tests

The static and analysis tests do not require Docker:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
for file in scripts/*.sh; do bash -n "$file"; done
```

The cluster tests require Docker and are the benchmark itself. A report is withheld if a correctness, isolation, completeness, configuration, runtime-quota, or sampler gate fails.

## Repository note

Choose and add a software license before distributing this package as a standalone repository. Without an explicit license, recipients can inspect the code but do not automatically receive broad reuse rights.
