# Methodology

## Experimental unit and design

One newly created kind cluster is one experimental unit. The exact published matrix has four configurations (`baseline`, `kyverno`, `gatekeeper`, and `native`) and three trials, for 12 cells. Only one cluster exists at a time. Engine order rotates by one position between trials.

The baseline is the same Kubernetes node image without a benchmark policy engine. All policy-engine cells use the contract in [`policies/CONTRACT.md`](policies/CONTRACT.md). The harness refuses to aggregate a new run unless every expected cell, request row, semantic check, isolation check, input hash, and resource sample is present.

## Load

Each cell receives a 15-second warm-up at 50 requests per second, then exactly 12,000 dry-run Pod `CREATE` requests offered at 200 requests per second for 60 seconds. Maximum client concurrency is 128. The client sends directly to `kube-apiserver` using the credentials from the kind kubeconfig.

Kubernetes documents that [`dryRun=All`](https://kubernetes.io/docs/reference/using-api/api-concepts/#dry-run) executes the relevant admission controllers, post-mutation validation, defaulting, schema validation, and patch merging, but skips persistence and side effects. This benchmark therefore measures the admission request path while avoiding downstream Pod lifecycle work.

## Correctness

Before load, 20 iterations of nine semantic cases produce 180 recorded cases. The revised shareable harness repeats those checks after load. The checks compare admission decisions, mutation results, no-overwrite behavior, and rejection messages with a predefined truth table. Helper requests used to seed and read `UPDATE` objects are not counted among the 180 recorded cases.

## Latency

The scheduler assigns each request an absolute deadline on Python's monotonic `perf_counter()` clock.

- `queue_delay_ms`: worker start minus planned send time.
- `http_latency_ms`: worker function start through full response-body completion. For historical compatibility this includes request-object construction and JSON encoding.
- `scheduled_latency_ms`: response completion minus planned send time, equal to queue delay plus client-observed service time.

Percentiles use linear interpolation at position `(N - 1) * q` (the common R-7/NumPy default definition). Pooled values contain 36,000 successful rows per configuration, but inference must use the three fresh-cluster trials rather than treating request rows as independent replicates.

Achieved RPS is successful requests divided by the offer window plus executor drain time. Scheduled latency and queue delay are the primary overload indicators.

## Resources

The harness samples once per second during 15 seconds of idle time and during the 60-second load window plus one second. The primary comparison is the entire kind-node CPU usage and memory working set because native policy evaluation runs inside `kube-apiserver` and cannot be isolated as a separate policy container.

The secondary view sums all running containers in the Kyverno or Gatekeeper namespace. It also records `kube-apiserver` separately, but that process includes all API-server work. CPU and memory means are calculated per trial and then averaged across trials. The resource p95 column is the arithmetic mean of the three per-trial p95 values.

## Reproducibility gates

New runs capture and validate:

- exact tool, chart, Kubernetes node-image, policy, configuration, and script hashes;
- resolved Helm values and rendered manifests;
- resolved container image inventory;
- cluster identity and exact trial order;
- expected policies, constraints, mutators, webhooks, deployments, and replica counts;
- empty admission-container resource requests and limits;
- absence of a Docker CPU quota on the kind node;
- raw request completeness and per-row latency accounting;
- pre-load and post-load semantic equivalence and isolation.

These additions improve provenance without changing the published request workload or policy configuration.

Kubernetes 1.36 documents `MutatingAdmissionPolicy` as [stable and in-process](https://kubernetes.io/docs/reference/access-authn-authz/mutating-admission-policy/), while `ValidatingAdmissionPolicy` has been [stable since 1.30](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/). Kubernetes also documents that [mutating webhooks run sequentially while matching validating webhooks can run in parallel](https://kubernetes.io/docs/concepts/cluster-administration/admission-webhooks-good-practices/#performance-and-latency). Those architectural differences are intentionally part of the end-to-end comparison.
