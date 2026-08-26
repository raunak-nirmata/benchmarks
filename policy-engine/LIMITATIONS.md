# Limitations and interpretation

This package freezes the experiment that produced the article. It intentionally does not rewrite that experiment after seeing its results.

The following limitations matter when interpreting or reproducing it:

- It is a three-replicate, 60-second pilot microbenchmark on one four-core host, not a production sizing study or a general product ranking.
- A fresh kind cluster is created for each engine/trial cell, but the clusters, Docker daemon, load generator, and resource sampler share one host and run sequentially.
- Historical `docker info` reported two running containers during each cell, while kind topology accounts for one control-plane container. Their identities were not retained, so the second container's effect cannot be ruled out. New runs capture the full container list and can enforce `REQUIRE_EXCLUSIVE_DOCKER=true`.
- Mutation and validation are measured together. The performance workload contains matched, valid, dry-run Pod `CREATE` requests only.
- Gatekeeper's generated webhooks used `failurePolicy: Ignore`, with one-second mutation and three-second validation timeouts. Kyverno used `Fail` with ten-second timeouts; native policies used `Fail`. No measured request failed, so this difference did not exercise fail-open behavior, but failure semantics were not normalized.
- Gatekeeper audit was disabled, but the published Helm values retained `controllerManager.disableGenerateOperation: false`. Kyverno's background, cleanup, reports, and report generation were disabled, while chart 3.8.2's admission-controller default `--generateValidatingAdmissionPolicy=true` remained enabled. The captured Kyverno cell contained no generated native admission policies.
- No admission-container CPU or memory requests/limits were configured. The historical run did not capture cgroup throttling, CPU pressure, frequency, thermals, or steal time. The revised harness now records node quota and throttling evidence for new runs.
- Warm-up and measurement use separate client processes, so the measured process starts with a new connection pool.
- The published `http_latency_ms` timer starts when a worker begins preparing the request and ends after the full response body is read. It therefore includes small client-side Pod construction and JSON serialization costs in addition to the HTTPS round trip.
- Resource samples cover the offered-load duration plus one second. Kyverno trial 3 drained for about 3.58 seconds after the 60-second offer window, so its final approximately 2.6 seconds were not sampled.
- Kyverno was non-stationary at 200 offered requests per second on this host. Its fresh-cluster HTTP p95 values were 66.76, 758.36, and 1,629.04 ms. The result demonstrates saturation for this configuration and workload; it does not establish the cause.
- Three trials do not support narrow confidence intervals. The cluster trial is the experimental unit; the 36,000 pooled requests per configuration are correlated observations, not 36,000 independent experiments.
- With four configurations and only three trials, the deterministic rotation is incomplete: no configuration occupies all four order positions. A confirmatory run needs complete balanced blocks and a recorded randomization seed.
- Chart archives are now checksum-pinned, and new runs capture resolved values, manifests, image IDs, input hashes, and cluster identities. The historical run did not capture all of those artifacts.
- The historical effective configuration named Docker CLI 28.3.3, but `host.txt` proves the client actually used was 29.1.3 because the bootstrap preferred an already-installed system client. The article reports Docker Engine 28.2.2, which is correct. The shareable profile records the actual client and server separately.

For a confirmatory study, first run a rate sweep and choose a common rate at which every engine meets a predeclared queue-delay and achieved-throughput gate. Use separate validate-only, mutate-only, and combined scenarios; normalize failure policies and timeouts; use balanced randomized blocks with more fresh-cluster replicates; and place the load generator on separate, controlled CPUs or a second host.
