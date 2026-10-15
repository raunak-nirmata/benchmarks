# Run presets

A preset is a named set of the shape knobs for one kind of run: which engines,
how many policies, which policy variant, which payload, and at what rate. It
exists so a published number can be reproduced from a name instead of from a
shell invocation someone has to reconstruct.

Run one with:

```bash
PRESET=five-arm-batched-n16 ./scripts/run-benchmark.sh results/my-run
```

A preset is sourced before `config/benchmark.env`, and every value there is a
`${VAR:-default}`, so the preset wins over the default and the environment wins
over the preset:

```bash
# the preset's engines and count, but one trial instead of three
PRESET=five-arm-batched-n16 TRIALS=1 ./scripts/run-benchmark.sh results/smoke
```

The chosen preset is recorded in the run's `effective-config.env`.

## What belongs in a preset

Shape only: `ENGINES`, `POLICY_COUNT`, `POLICY_MODE`, `KYVERNO_CEL_VARIANT`,
`PAYLOAD_PROFILE`, `PAYLOAD_PADDING_BYTES`, `TARGET_RPS`, `TRIALS`, `PROFILE`.

Build overrides - `KYVERNO_CHART_PATH` and the image variables - deliberately do
**not** belong here. They are absolute paths on one machine, so committing them
would make a preset unreproducible for anyone else. Pass them in the environment:

```bash
PRESET=five-arm-batched-n16 \
  KYVERNO_CHART_PATH=/path/to/kyverno/charts/kyverno \
  KYVERNO_IMAGE_REGISTRY=local KYVERNO_IMAGE_REPOSITORY=kyverno KYVERNO_IMAGE_TAG=after \
  KYVERNO_INIT_IMAGE_REPOSITORY=kyvernopre KYVERNO_INIT_IMAGE_TAG=after \
  ./scripts/run-benchmark.sh results/after
```

## Available presets

| Preset | Engines | Count | Variant | Payload | Rate |
|---|---|---:|---|---|---:|
| `five-arm-published` | all five | 1 | matchconditions | minimal | 200 |
| `five-arm-batched-n16` | all five | 16 | selectors | minimal | 200 |
| `cel-ab-fanout-n16` | baseline, kyverno-cel | 16 | matchconditions | minimal | 200 |
| `cel-ab-batched-n16` | baseline, kyverno-cel | 16 | selectors | minimal | 200 |
| `cel-ab-realistic-n16` | baseline, kyverno-cel | 16 | selectors | realistic | 150 |
| `legacy-ab-realistic-n16` | baseline, kyverno | 16 | n/a | realistic | 10 |
| `five-arm-validate-n16` | all five | 16 | selectors | minimal | 200 |
| `cel-ab-validate-n16` | baseline, kyverno-cel | 16 | selectors | minimal | 200 |
| `five-arm-mutate-n16` | all five | 16 | selectors | minimal | 200 |
| `cel-mutate-distinct-n16` | baseline, kyverno-cel | 16 | distinct | minimal | 200 |

The last two set `POLICY_MODE=validate`, which installs only the validating half
of the contract. See [`../../SCALING.md`](../../SCALING.md) for why that is the
controlled shape for comparing engines or builds.

None of these is an article reproduction. To reproduce the published numbers,
use no preset at all - the defaults in `config/benchmark.env` are the published
profile.
