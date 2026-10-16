# Reproducing the policy-engine measurements

This walks through a clean machine to a finished result, for the two runs that
matter most: every engine measured on the validating path alone, and every engine
measured on the mutating path alone.

Splitting the two halves is the point. The published profile measures mutation
and validation together, and the two paths behave very differently - in our runs
Gatekeeper mutated for less than it validated, while Kyverno's legacy engine
spent nine times more on mutation than on validation. A combined number hides
that.

Everything below defaults to the published profile. A run with no preset still
reproduces the original article's shape exactly.

## 1. A machine to run on

You need Linux with Docker, four or more cores, about 8 GiB of RAM, and 10 GiB
free for container images. The measurements are host-sensitive, so results are
comparable within one run, not against numbers from a different machine.

### On Linux already

Skip to step 2. Check that your user can talk to the Docker daemon:

```bash
docker info >/dev/null && echo ok
```

### On macOS, through Lima

Docker Desktop's VM is shared with everything else you run, so we used a
dedicated Lima VM instead. Nothing else runs in it.

```bash
brew install lima
limactl create --name policy-bench --cpus 8 --memory 16 --disk 40 template://ubuntu-lts
limactl start policy-bench
limactl shell policy-bench
```

Inside the VM, install Docker and give yourself access to it:

```bash
sudo apt-get update
sudo apt-get install -y docker.io git python3 curl
sudo usermod -aG docker "$USER"
```

Group membership does not apply to a session that is already open, and leaving
with `exit` is often not enough because Lima reuses the connection. Restart the
VM:

```bash
exit
limactl stop policy-bench
limactl start policy-bench
limactl shell policy-bench
docker info >/dev/null && echo ok
```

`newgrp docker` works too if you would rather not restart. Either way, confirm
with `id -nG | tr ' ' '\n' | grep docker` before continuing - without it every
Docker call fails with `permission denied while trying to connect to the Docker
API at unix:///var/run/docker.sock`.

**Keep the host awake for the whole run.** A sleeping laptop suspends the VM
mid-measurement. The harness detects the resulting gap in its resource samples
and refuses to report the run, so a sleep costs you the whole thing. On macOS:

```bash
caffeinate -d
```

## 2. Get the harness and its tools

**Clone inside the VM's own filesystem, not into the mounted macOS home.** Lima
mounts your Mac home read-only, so a run started from `/Users/<you>/...` fails
once it tries to create `.tools/` or write `results/`. If you already have the
repository on your Mac, clone it again here rather than using the mounted copy.

```bash
cd ~                 # /home/<you>.linux inside the VM, not /Users/<you>
git clone https://github.com/merdincz/benchmarks.git
cd benchmarks/policy-engine
./scripts/bootstrap-tools.sh
```

Results then live inside the VM. To copy a finished run back to the Mac, from a
Mac shell:

```bash
limactl shell policy-bench -- bash -lc \
  'cd ~/benchmarks/policy-engine/results/validate-n16 && tar cf - summary.json REPORT.md' \
  > /tmp/result.tar && tar xf /tmp/result.tar -C ~/Desktop/
```

`bootstrap-tools.sh` downloads pinned kind, kubectl, and helm into
`policy-engine/.tools/` and checks the Helm chart archives against frozen
SHA-256 digests. It touches nothing outside this directory.

Check the tests pass before measuring anything:

```bash
python3 -m unittest discover -s tests -q
```

## 3. The two runs

Each is five arms - no-policy baseline, Kyverno's legacy ClusterPolicy engine,
Kyverno's CEL policy types, Gatekeeper, and native admission policies - with 16
policy pairs per arm, three fresh single-node clusters per arm, and 12,000
measured requests per cell. Budget about 50 minutes each.

### Validating path only

```bash
PRESET=five-arm-validate-n16 ./scripts/run-benchmark.sh results/validate-n16
```

### Mutating path only

```bash
PRESET=five-arm-mutate-n16 ./scripts/run-benchmark.sh results/mutate-n16
```

### Or both halves and the combination, from one command

```bash
PRESET=five-arm-validate-n16 ./scripts/run-mode-sweep.sh results/sweep
```

That runs the validating half, the mutating half, and both together, then writes
`results/sweep/MODES.md` with all three tables and the ratio between the halves.
About 2.5 hours. It is the better option if you want the comparison, because all
three runs then share one host and one build, which is what makes the ratio mean
anything. `compare-modes.py` verifies that for itself and says so in the report
if the runs drifted apart.

## 4. Reading the result

Each run writes:

| File | What it holds |
|---|---|
| `REPORT.md` | The tables, the validity gates, the interpretation limits |
| `summary.json` | The same, machine readable, with per-trial detail |
| `trial-N/<arm>/requests.csv` | Every measured request and its latency |
| `trial-N/<arm>/resources-load.jsonl` | One resource sample per second during load |
| `trial-N/<arm>/rendered/policies.yaml` | The policies actually applied to that cluster |
| `trial-N/<arm>/webhooks.yaml` | The webhook configurations the engine generated |

Start with the `Admission data-path containers` table in `REPORT.md`. Engine CPU
during load is the number that separates the engines; client latency also carries
the API server and the network, so it moves less.

```bash
sed -n '/^## Admission data-path containers/,/^## CPU work/p' \
  results/validate-n16/REPORT.md
```

That prints a row per arm, for example:

```
| kyverno-cel | all containers in engine namespace | 0.173 | 0.197 | 69.91 | 70.23 |
| native | kube-apiserver (policy is in-process; not separable) | 0.691 | ...
```

**Check the gates before believing anything.** Every cell must report 180/180
semantic cases, pass equivalence and isolation, and complete 12,000/12,000
requests:

```bash
grep -c 'pass | pass' results/validate-n16/REPORT.md    # expect 15
```

A run that fails a gate produces no `REPORT.md` at all. That is deliberate: the
harness refuses to publish a number it cannot stand behind, rather than
publishing it with a footnote.

## 5. What you should see

From our runs on an 8 vCPU aarch64 host, engine CPU in cores during load:

| arm | validating | mutating |
|---|---:|---:|
| gatekeeper | 0.273 | 0.175 |
| kyverno-cel | 0.173 | 0.484 |
| kyverno (legacy) | 0.343 | 3.073 |

Absolute numbers will differ on your host. The shape should not: Kyverno's CEL
engine is the cheapest validator measured, Gatekeeper is the only engine that
mutates for less than it validates, and the legacy engine's mutating path is an
order of magnitude above everything else.

## 6. Two things to know before interpreting

**Kyverno's webhook shape decides whether policies are batched.** A CEL policy
that declares `matchConditions` gets its own webhook endpoint, so sixteen such
policies produce sixteen webhook calls carrying one policy each. Expressed
through `matchConstraints` selectors instead, the same sixteen share one webhook
and arrive in a single call. These presets use the selectors form, which is the
shape Kyverno's own Pod Security Standards policies produce. Set
`KYVERNO_CEL_VARIANT=matchconditions` to measure the other one.

**The native arm's cost is in the API server, not in an engine.** Native
admission policies run in-process, so there is no engine container to measure.
Compare their `apiserver` column against the baseline's rather than looking for
an engine number.

## 7. Other shapes

```bash
./scripts/run-benchmark.sh results/published            # the original article profile
PRESET=five-arm-published ./scripts/run-benchmark.sh results/five-arm-n1
PRESET=cel-ab-validate-n16 ./scripts/run-benchmark.sh results/ab   # one engine, two builds
```

`config/presets/README.md` lists every preset. `SCALING.md` explains the knobs
behind them.
