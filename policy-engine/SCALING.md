# Policy-count scaling and engine A/B runs

The published profile installs one mutate and one validate policy per arm. That
is the right shape for comparing engines on a typical single-policy admission
path, but it cannot see costs that grow with the number of policies matching the
same object or with the size of that object, and it cannot compare two builds of
the same engine.

Three knobs in [`config/benchmark.env`](config/benchmark.env) cover these cases.
None of them changes the published defaults.

| Knob | Default | Effect |
|---|---|---|
| `POLICY_COUNT` | `1` | Copies of the contract policy pair installed per arm. |
| `PROFILE` | `exact` | `exact` requires the four published engines; `scaling` allows any subset that still contains `baseline`. |
| `PAYLOAD_PADDING_BYTES` | `0` | Bytes of inert annotation added to every Pod the client sends, which is how the admitted object grows. |
| `KYVERNO_CEL_VARIANT` | `matchconditions` | Which policy set the `kyverno-cel` arm installs. `selectors` keeps the policies in one shared webhook. |
| `PAYLOAD_PROFILE` | `minimal` | Shape of the Pod the client sends. `realistic` is the shape a Deployment produces. |
| `POLICY_MODE` | `both` | Which halves of the contract each arm installs: `both`, `validate`, or `mutate`. |

Any run with `POLICY_COUNT` above 1, `PAYLOAD_PADDING_BYTES` above 0, a non-`exact`
profile, a local chart, or an image override is **not** an article reproduction. `summary.json` records this as
`article_reproduction_shape: false`, `REPORT.md` says so at the top, and the
comparison against the frozen reference is skipped rather than printed
misleadingly.

## Validate-only runs

`POLICY_MODE=validate` installs only the validating half of the contract on every
arm; `mutate` installs only the mutating half. Both are filtered per document, so
a Gatekeeper validate-only cell keeps its `ConstraintTemplate` (which defines the
kind its Constraints instantiate) and drops only the `AssignMetadata` objects.

Use `validate` when comparing engines or builds. With `both`, the two
`kyverno-cel` variants do not perform the same mutating work:

| variant | webhook calls | validating executions | mutating executions |
|---|---:|---:|---:|
| selectors (batched) | 25,829 | 206,560 | 206,560 |
| matchconditions (fan-out) | 219,419 | 206,560 | 12,850 |

The matchConditions variant's mutate policy carries an
`annotation-not-already-present` match condition. Mutating webhooks run in
sequence over the progressively patched object, so the first copy adds the
annotation and the remaining copies match nothing and never execute. The
selectors variant had to move that add-only logic into the patch value, so every
copy executes.

The validating half has no such condition and executes every copy in both
variants, which makes a validate-only run a controlled comparison: identical
work, done as one call carrying 16 policies or as 16 calls carrying one.

The semantic gate follows the mode. A validate-only run expects no injected
annotation, and a mutate-only run expects no denial, so the 180 cases per cell
still assert the right outcome rather than being relaxed.

## Running both halves and the combination in one go

`scripts/run-mode-sweep.sh` runs the same configuration three times - validating
half, mutating half, then both - and writes one comparison across them:

```bash
PRESET=five-arm-validate-n16 ./scripts/run-mode-sweep.sh results/sweep
```

The preset supplies every knob except the mode, which the sweep overrides per
run, so any preset works as the base. Output:

```
results/sweep/validate/   full benchmark run
results/sweep/mutate/     full benchmark run
results/sweep/both/       full benchmark run
results/sweep/MODES.md    all three tables plus the mutate/validate ratio
results/sweep/modes.json  the same, machine readable
```

Three separate runs rather than one, because a cell installs its policies when
its cluster is created. Driving them from one invocation is what keeps the host,
the build and the policy count identical - which is the thing that makes the
ratio between the halves meaningful. `compare-modes.py` checks that for itself
and prints a warning in `MODES.md` if any of them drifted, rather than dividing
two numbers that should not be divided.

Expect about 2.5 hours for a five-arm sweep at three trials. Use
`POLICY_MODES_SWEEP` to run a subset.

## Presets

Setting these knobs by hand on the command line means a result can only be
reproduced by someone who still has the invocation. A preset names one run shape
so the invocation is the name:

```bash
PRESET=five-arm-batched-n16 ./scripts/run-benchmark.sh results/my-run
```

Presets live in [`config/presets/`](config/presets/), are sourced before
`config/benchmark.env`, and defer to anything already set in the environment, so
an explicit override still wins. The chosen preset is recorded in the run's
`effective-config.env`. See [`config/presets/README.md`](config/presets/README.md)
for the list and for why build overrides are deliberately not in them.

Running with no preset reproduces the published profile.

## How the policy count works

`scripts/render-policies.py` renders each arm's templates into the cell's own
`rendered/` directory, which keeps `policies/` byte-stable — `hash-inputs.sh`
hashes that tree and the run aborts if it changes mid-run.

At `POLICY_COUNT=1` the templates are copied byte for byte. Above 1, every
policy name gains a zero-padded suffix and nothing else changes: match blocks,
the mutation value, and the denial message stay identical, and binding
references (`policyName:`) follow the rename. A Gatekeeper ConstraintTemplate
defines the constraint kind, so it stays single while its Constraints and
AssignMetadata objects multiply.

The [CONTRACT.md](policies/CONTRACT.md) truth table still holds at any count:
the mutations are add-if-absent and therefore idempotent, and the denial
assertion is a substring check, so aggregated messages from several identical
policies still pass.

### Fairness

If you publish cross-engine numbers at `POLICY_COUNT` above 1, every arm must
scale, which is why the renderer handles all four. Comparing 16 Kyverno policies
with 1 Gatekeeper constraint would be meaningless. A single-engine A/B is the
exception: the other arms are constants, so running them costs wall clock and
adds nothing.

## Why payload size is a second axis

The published Pod is about 320 bytes serialized; `summary.json` records the
exact size for each run. Costs an engine pays per policy for
re-reading or re-copying the admitted object scale with that size as well as with
the policy count, so a policy-count sweep at the default payload cannot see them:
sixteen avoided round-trips of a ~320-byte object are microseconds.

`PAYLOAD_PADDING_BYTES` adds a single inert annotation of that many bytes. No arm
matches on it, mutates it, or reads it, and real clusters carry payloads of exactly
this shape (`kubectl.kubernetes.io/last-applied-configuration`). At 0 the Pod is
byte-identical to the published one. The semantic probes use the same size as the
measured requests, so the contract is verified at the size being measured.

Measured on this harness: at 16 policy pairs and the default payload, reverting
Kyverno's per-policy context work (#17565, #17572) changed legacy engine CPU by
-3.8% and the CEL arm not at all. That is a statement about a ~320-byte object, not
about the change.

## Why one webhook call per policy defeats a policy-count sweep

Kyverno's webhook controller (`buildWebhookRules`, `pkg/controllers/webhook/validating.go`)
splits CEL policies two ways. A policy that declares `matchConditions` — or sets
`matchPolicy: Exact`, or `timeoutSeconds` — is *fine-grained* and gets its own
webhook endpoint, `/vpol/<name>`. Every other policy is *basic*, and all policies
sharing a selector pair go into one webhook whose path lists every name,
`/vpol/<name1>/<name2>/...`. The handler splits that path and evaluates all of them
in a single engine call.

So the published policy set, which declares `matchConditions`, produces **N
AdmissionReview calls carrying one policy each**. The engine's per-request policy
loop is always N=1, no matter how high `POLICY_COUNT` is. Anything that amortizes
work across policies within one request is unmeasurable in that shape.

`KYVERNO_CEL_VARIANT=selectors` installs `policies/kyverno-cel-selectors`, which
expresses the same contract through `matchConstraints.namespaceSelector` and
`objectSelector`. Neither is a fine-grained trigger, so all N policies share one
webhook and one call carries all of them.

Measured on this harness at 16 policy pairs: the default set issues roughly 17
webhook calls per Pod CREATE (`kyverno_admission_requests_total` 218,830 against
about 12,750 Pod creates), each re-parsing the admitted object. Comparing the two
variants at the same policy count measures what that fan-out costs.

## What a realistic configuration looks like

Three of this harness's defaults are unrepresentative of a normal Kyverno install,
and all three suppress per-policy costs. None of this is a criticism of the
published profile: it was built to compare engines on a single-policy path, and it
does that well.

**Policy shape.** Every one of the 19 CEL policies in Kyverno's own
`kyverno-policies` chart declares no `matchConditions`, no `matchPolicy: Exact`,
and no `timeoutSeconds` — so all of them land in the shared webhook and a single
AdmissionReview evaluates the whole set. The contract policy in `policies/kyverno-cel`
declares `matchConditions`, which is what mirrors the native VAP arm, but it puts
the arm in the one-webhook-per-policy shape that no standard policy set produces.
`KYVERNO_CEL_VARIANT=selectors` matches the shipped policies.

**Policy count.** The PSS baseline plus restricted CEL set is 19 policies, so
`POLICY_COUNT` in the mid-teens is an ordinary install, not a stress test.

**Payload.** The published Pod is one `pause` container with no resources, probes,
volumes, or service account: about 300 bytes serialized. `PAYLOAD_PROFILE=realistic`
sends the shape a Deployment produces for an ordinary service — two containers,
requests and limits, both probes, a PSS-restricted security context, a service
account, and config, secret, and emptyDir mounts — which comes out near 3.8 KB.
Nothing in it is chosen for size, there is no filler field, and tests assert both
of those. A Pod created by `kubectl apply` would additionally carry
`kubectl.kubernetes.io/last-applied-configuration` and be roughly twice as large,
so this fixture is on the conservative side.

`PAYLOAD_PADDING_BYTES` remains available for sensitivity sweeps, but a headline
number should come from a profile, not from padding.

## Measuring one engine before and after a change

Latency through `kube-apiserver` to a webhook and back is milliseconds, and a
per-policy evaluation saving is microseconds, so on a shared host a latency
comparison is mostly noise. Use **CPU work per request** instead:
`REPORT.md`'s "CPU work per admission request" table and the matching
`cpu_seconds_per_1k_requests` fields in `summary.json`. The `engine` category
covers only the engine's own namespace, so it isolates policy evaluation from
apiserver work.

`engine-metrics-{before,after}.prom` in each cell holds the engine's own
Prometheus scrape. For Kyverno that includes admission-duration histograms,
which are webhook-internal, and the admission request counters, which confirm
that every installed policy really fired.

### Both builds from one tree

Comparing a published chart with a locally built one measures every commit
between them. To isolate one change, build two images from the same tree — the
second with that change reverted — and install both from the same local chart:

```bash
# in a kyverno checkout, for each build:
KO_DOCKER_REPO=ko.local make ko-build-all
docker tag ko.local/kyverno:latest policy-bench/kyverno:after
docker tag ko.local/kyvernopre:latest policy-bench/kyvernopre:after
kind load docker-image policy-bench/kyverno:after --name policy-bench-kyverno
```

Then point the harness at the local chart and the loaded images:

```bash
export PROFILE=scaling
export ENGINES="baseline kyverno kyverno-cel"
export POLICY_COUNT=16
export KYVERNO_CHART_PATH=/path/to/kyverno/charts/kyverno
export KYVERNO_IMAGE_REGISTRY=policy-bench
export KYVERNO_IMAGE_REPOSITORY=kyverno
export KYVERNO_IMAGE_TAG=after
export KYVERNO_INIT_IMAGE_REPOSITORY=kyvernopre
export KYVERNO_INIT_IMAGE_TAG=after
./scripts/run-benchmark.sh results/after-n16
```

The image tag is the only variable that changes between the two runs. Interleave
the runs in one sitting: a host that warms up or gets busy between them will
move the numbers more than most code changes do.

### Reading the results

Compare before with after only within a fixed engine, policy count, and offered
rate. Never compare across policy counts: at a higher count the same offered rate
is more work, so a cell may become queue-bound, which changes what latency means.

If a high policy count saturates the host, the load generator will report failed
or incomplete requests and the run will withhold its report by design. Lower
`TARGET_RPS` for that pair and keep it equal across both builds.
