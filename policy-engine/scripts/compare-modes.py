#!/usr/bin/env python3
"""Compare the validating and mutating halves of one configuration.

Reads the per-mode runs a sweep produced and writes one report placing every arm
in each half, plus the ratio between them. The ratio is the point: it says where
an engine spends its admission budget, and it is not the same for every engine.

Only compares runs that share a host, a build, a policy count and a load shape,
because a cross-run ratio is meaningless otherwise.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

MODES = ("validate", "mutate", "both")
ARM_ORDER = ("baseline", "native", "gatekeeper", "kyverno-cel", "kyverno")
# Config keys that must agree for a cross-mode ratio to mean anything. The mode
# itself and the result directory are expected to differ.
COMPARABLE_KEYS = (
    "ENGINES", "POLICY_COUNT", "KYVERNO_CEL_VARIANT", "PAYLOAD_PROFILE",
    "PAYLOAD_PADDING_BYTES", "TARGET_RPS", "DURATION_SECONDS", "TRIALS",
    "KIND_NODE_IMAGE", "KYVERNO_IMAGE_TAG", "GATEKEEPER_CHART_VERSION",
)


def load(run_dir: Path) -> dict | None:
    summary = run_dir / "summary.json"
    if not summary.is_file():
        return None
    return json.loads(summary.read_text())


def mean_latency(engine: dict, percentile: str) -> float:
    return statistics.mean(t["http_latency_ms"][percentile] for t in engine["trial_runs"])


def engine_cpu(engine: dict) -> float:
    return engine["resources"].get("engine", {}).get("cpu_cores", {}).get("load_mean", 0.0)


def check_comparable(summaries: dict[str, dict]) -> list[str]:
    """Which config keys differ across modes. Empty means the ratio is sound."""
    differing = []
    for key in COMPARABLE_KEYS:
        values = {
            mode: summary["effective_config"].get(key) for mode, summary in summaries.items()
        }
        if len(set(values.values())) > 1:
            differing.append(f"{key}: " + ", ".join(f"{m}={v}" for m, v in values.items()))
    return differing


def table(summary: dict, title: str) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| arm | p50 | p95 | p99 | node CPU | apiserver | engine CPU | engine mem |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rows = []
    for arm, item in summary["engines"].items():
        resources = item["resources"]
        engine = resources.get("engine", {})
        rows.append(
            (
                mean_latency(item, "p50"),
                arm,
                mean_latency(item, "p95"),
                mean_latency(item, "p99"),
                resources["node"]["cpu_cores"]["load_mean"],
                resources["apiserver"]["cpu_cores"]["load_mean"],
                engine_cpu(item),
                engine.get("memory_bytes", {}).get("load_mean", 0) / 1e6,
            )
        )
    for p50, arm, p95, p99, node, api, eng, mem in sorted(rows):
        cpu = f"{eng:.3f}" if eng else "-"
        memory = f"{mem:.1f} MB" if mem else "-"
        lines.append(
            f"| {arm} | {p50:.2f} | {p95:.2f} | {p99:.2f} | {node:.3f} | "
            f"{api:.3f} | {cpu} | {memory} |"
        )
    lines.append("")
    return lines


def ratio_table(summaries: dict[str, dict]) -> list[str]:
    if not {"validate", "mutate"} <= summaries.keys():
        return []
    lines = [
        "### Where each engine spends its admission budget",
        "",
        "Engine CPU in cores. Arms with no engine process report their cost in the",
        "apiserver instead and are listed below the table.",
        "",
        "| arm | validate | mutate | mutate / validate |",
        "|---|---:|---:|---:|",
    ]
    footnotes = []
    for arm in ARM_ORDER:
        validate = summaries["validate"]["engines"].get(arm)
        mutate = summaries["mutate"]["engines"].get(arm)
        if not validate or not mutate:
            continue
        v, m = engine_cpu(validate), engine_cpu(mutate)
        if not v or not m:
            if arm != "baseline":
                footnotes.append(
                    f"- {arm}: no engine process; apiserver CPU "
                    f"{validate['resources']['apiserver']['cpu_cores']['load_mean']:.3f} "
                    f"validating, "
                    f"{mutate['resources']['apiserver']['cpu_cores']['load_mean']:.3f} mutating"
                )
            continue
        lines.append(f"| {arm} | {v:.3f} | {m:.3f} | {m / v:.1f}x |")
    lines.append("")
    lines.extend(footnotes)
    if footnotes:
        lines.append("")
    return lines


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare-modes.py SWEEP_DIR")
    sweep = Path(sys.argv[1])
    summaries = {}
    for mode in MODES:
        summary = load(sweep / mode)
        if summary is not None:
            summaries[mode] = summary
    if not summaries:
        raise SystemExit(f"no completed mode runs in {sweep}")

    first = next(iter(summaries.values()))
    config = first["effective_config"]
    lines = [
        "# Validating and mutating halves compared",
        "",
        f"- Policy count: {first.get('policy_count')}",
        f"- Kyverno CEL variant: {first.get('kyverno_cel_variant', 'matchconditions')}",
        f"- Payload: {first.get('payload_bytes')} bytes, profile "
        f"{first.get('payload_profile', 'minimal')}",
        f"- Load: {config.get('DURATION_SECONDS')}s at {config.get('TARGET_RPS')} rps, "
        f"{config.get('TRIALS')} trials per arm",
        f"- Modes present: {', '.join(summaries)}",
        "",
        "Latency in milliseconds, CPU in cores during load, both averaged across",
        "trials. Every mode ran from one invocation on one host, so the halves are",
        "comparable with each other.",
        "",
    ]

    differing = check_comparable(summaries)
    if differing:
        lines += [
            "> **These runs are not comparable.** The following differ between modes,",
            "> so the ratio below is not meaningful:",
            "",
        ] + [f"> - {item}" for item in differing] + [""]

    titles = {
        "validate": "Validating half only",
        "mutate": "Mutating half only",
        "both": "Both halves (what a cluster actually runs)",
    }
    for mode in MODES:
        if mode in summaries:
            lines += table(summaries[mode], titles[mode])
    lines += ratio_table(summaries)

    (sweep / "MODES.md").write_text("\n".join(lines) + "\n")
    (sweep / "modes.json").write_text(
        json.dumps(
            {
                "modes": {
                    mode: {
                        arm: {
                            "p50_ms": mean_latency(item, "p50"),
                            "p95_ms": mean_latency(item, "p95"),
                            "p99_ms": mean_latency(item, "p99"),
                            "engine_cpu_cores": engine_cpu(item),
                            "node_cpu_cores": item["resources"]["node"]["cpu_cores"]["load_mean"],
                            "apiserver_cpu_cores": item["resources"]["apiserver"]["cpu_cores"]["load_mean"],
                        }
                        for arm, item in summary["engines"].items()
                    }
                    for mode, summary in summaries.items()
                },
                "comparable": not differing,
                "differing_config": differing,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"[policy-bench] wrote {sweep / 'MODES.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
