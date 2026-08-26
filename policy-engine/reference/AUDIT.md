# Published-result audit

The retained run at `results/benchmark-20260819-exact` passed an independent raw-data audit on 2026-08-26.

- 12 expected cells were present: four configurations times three fresh-cluster trials.
- Each cell contained 12,000 indexed rows on the expected 5 ms offered schedule.
- All 144,000 measured responses were HTTP 201 and passed the engine-specific mutation check.
- Every cell recorded 180/180 passing semantic cases and a passing historical isolation gate.
- Every stored latency percentile recomputed from raw rows using linear interpolation at `(N - 1) * q`.
- Every published node CPU/memory figure recomputed from the raw one-second JSONL samples.
- All article values match the independent results after display rounding.
- The historical `summary.json` and `REPORT.md` match their frozen SHA-256 hashes.

| Configuration | HTTP p50 | HTTP p95 | HTTP p99 | Scheduled p95 | Queue p95 |
|---|---:|---:|---:|---:|---:|
| Baseline | 4.15914 ms | 7.60379 ms | 14.27313 ms | 8.29375 ms | 0.87065 ms |
| Native | 6.38149 ms | 12.77090 ms | 24.37861 ms | 13.72769 ms | 1.34621 ms |
| Gatekeeper | 8.76120 ms | 19.72385 ms | 51.83129 ms | 20.23109 ms | 1.00563 ms |
| Kyverno | 25.03323 ms | 1,198.86263 ms | 1,892.24856 ms | 3,935.94680 ms | 3,247.45959 ms |

This confirms arithmetic and evidence integrity, not external validity. The historical isolation gate was narrower than the revised package, fresh cluster UIDs were not captured, and CPU throttling/image digests/resolved manifests were not retained. Every historical `host.txt` also reports two running Docker containers while kind accounts for one; their identities were not captured. Fresh-cluster creation therefore also relies on the reviewed orchestration path. See [`../LIMITATIONS.md`](../LIMITATIONS.md).
