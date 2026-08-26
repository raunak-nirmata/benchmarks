# Policy contract

The policy files are syntactically different because each engine has a different language. Fairness is defined by the observable contract below.

| Case | Namespace | Subject label | Team annotation | Existing injected annotation | Expected decision | Expected injected annotation |
|---|---|---|---|---|---|---|
| Matched valid | `policy-bench` | `true` | `platform` | absent | allow | `true` |
| Matched missing team | `policy-bench` | `true` | absent | absent | deny | n/a |
| Matched wrong team | `policy-bench` | `true` | other | absent | deny | n/a |
| Unmatched label | `policy-bench` | absent | absent | absent | allow | absent |
| Unmatched namespace | `policy-bench-control` | `true` | absent | absent | allow | absent |
| No overwrite | `policy-bench` | `true` | `platform` | `false` | allow | `false` |

The first six cases are checked for `CREATE`. Matched-valid, matched-wrong-team, and no-overwrite are also checked for `UPDATE`, producing nine recorded cases per iteration. The published profile runs 20 iterations, or 180 recorded cases before load in every cluster.

The measured performance request is only the first row: a matched, valid, dry-run Pod `CREATE` with mutation and validation enabled together. The other rows and `UPDATE` are correctness checks, not performance workloads.

The denial message is:

```text
policy-bench-team annotation must equal platform
```
