#!/usr/bin/env python3
"""Compare a newly aggregated run with the article's frozen reference metrics."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


ENGINES = ("baseline", "native", "gatekeeper", "kyverno")


def percent_change(candidate: float, reference: float) -> float:
    if reference == 0:
        return math.nan
    return (candidate / reference - 1.0) * 100.0


def fmt_change(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value:+.1f}%"


def candidate_metrics(summary: dict[str, Any], engine: str) -> dict[str, float]:
    item = summary["engines"][engine]
    node = item["resources"]["node"]
    return {
        "HTTP p50 ms": item["http_latency_ms"]["p50"],
        "HTTP p95 ms": item["http_latency_ms"]["p95"],
        "HTTP p99 ms": item["http_latency_ms"]["p99"],
        "Scheduled p95 ms": item["scheduled_latency_ms"]["p95"],
        "Queue p95 ms": item["client_queue_delay_ms"]["p95"],
        "Achieved RPS": item["achieved_rps"]["mean"],
        "Node CPU mean cores": node["cpu_cores"]["load_mean"],
        "Node memory mean MiB": node["memory_bytes"]["load_mean"] / 1024 / 1024,
    }


def reference_metrics(reference: dict[str, Any], engine: str) -> dict[str, float]:
    item = reference["engines"][engine]
    return {
        "HTTP p50 ms": item["http_latency_ms"]["p50"],
        "HTTP p95 ms": item["http_latency_ms"]["p95"],
        "HTTP p99 ms": item["http_latency_ms"]["p99"],
        "Scheduled p95 ms": item["scheduled_latency_p95_ms"],
        "Queue p95 ms": item["queue_delay_p95_ms"],
        "Achieved RPS": item["achieved_rps_mean"],
        "Node CPU mean cores": item["node"]["cpu_load_mean_cores"],
        "Node memory mean MiB": item["node"]["memory_load_mean_bytes"] / 1024 / 1024,
    }


def profile_checks(reference: dict[str, Any], summary: dict[str, Any]) -> list[dict[str, Any]]:
    config = summary.get("effective_config", {})
    load = summary.get("load_shape", {})
    expected = reference["load_shape"]
    actual = {
        "trials": int(config.get("TRIALS", -1)),
        "duration_seconds": float(load.get("duration_seconds", -1)),
        "target_rps": float(load.get("target_rps", -1)),
        "concurrency": int(load.get("concurrency", -1)),
        "requests_per_engine_per_trial": int(
            load.get("requests_per_engine_per_trial", -1)
        ),
        "warmup_seconds": float(config.get("WARMUP_SECONDS", -1)),
        "warmup_rps": float(config.get("WARMUP_RPS", -1)),
        "idle_sample_seconds": float(config.get("IDLE_SAMPLE_SECONDS", -1)),
        "settle_seconds": float(config.get("SETTLE_SECONDS", -1)),
    }
    checks = [
        {
            "field": field,
            "reference": expected[field],
            "candidate": actual[field],
            "matches": actual[field] == expected[field],
        }
        for field in expected
    ]
    reference_environment = reference.get("environment", {})
    candidate_environment = summary.get("execution_host", {})
    environment_actual = {
        "operating_system": candidate_environment.get("operating_system"),
        "architecture": candidate_environment.get("architecture"),
        "cpu_count": candidate_environment.get("cpu_count"),
        "cpu_model": candidate_environment.get("cpu_model"),
        "total_memory": candidate_environment.get("total_memory"),
        "docker_client_version": candidate_environment.get("docker_client_version"),
        "docker_server_version": candidate_environment.get("docker_server_version"),
        "kind_version": config.get("KIND_VERSION"),
        "kubectl_version": config.get("KUBECTL_VERSION"),
        "helm_version": config.get("HELM_VERSION"),
        "kind_node_image": config.get("KIND_NODE_IMAGE"),
        "kyverno_chart_version": config.get("KYVERNO_CHART_VERSION"),
        "gatekeeper_chart_version": config.get("GATEKEEPER_CHART_VERSION"),
    }
    for field, expected_value in reference_environment.items():
        actual_value = environment_actual.get(field)
        checks.append(
            {
                "field": f"environment.{field}",
                "reference": expected_value,
                "candidate": actual_value if actual_value is not None else "not recorded",
                "matches": actual_value == expected_value,
            }
        )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference")
    parser.add_argument("candidate_summary")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    reference = json.loads(Path(args.reference).read_text())
    candidate = json.loads(Path(args.candidate_summary).read_text())
    checks = profile_checks(reference, candidate)
    metric_rows = []
    for engine in ENGINES:
        ref_values = reference_metrics(reference, engine)
        new_values = candidate_metrics(candidate, engine)
        for metric in ref_values:
            metric_rows.append(
                {
                    "engine": engine,
                    "metric": metric,
                    "reference": ref_values[metric],
                    "candidate": new_values[metric],
                    "percent_change": percent_change(new_values[metric], ref_values[metric]),
                }
            )

    trial_rows = []
    for engine in ENGINES:
        reference_trials = reference["engines"][engine]["trial_http_p95_ms"]
        candidate_trials = [
            trial["http_latency_ms"]["p95"]
            for trial in candidate["engines"][engine]["trial_runs"]
        ]
        trial_rows.append(
            {
                "engine": engine,
                "reference": reference_trials,
                "candidate": candidate_trials,
                "reference_min": min(reference_trials),
                "reference_max": max(reference_trials),
                "candidate_min": min(candidate_trials),
                "candidate_max": max(candidate_trials),
            }
        )

    exact_profile = all(
        item["matches"] for item in checks if not item["field"].startswith("environment.")
    )
    exact_environment = all(
        item["matches"] for item in checks if item["field"].startswith("environment.")
    )
    result = {
        "reference": reference["label"],
        "candidate": str(Path(args.candidate_summary).resolve()),
        "exact_load_profile": exact_profile,
        "exact_reference_environment": exact_environment,
        "profile_checks": checks,
        "metrics": metric_rows,
        "trial_http_p95_ms": trial_rows,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")

    lines = [
        "# Comparison with the published run",
        "",
        f"Exact published load profile: **{'yes' if exact_profile else 'no'}**",
        f"Exact recorded host/tool environment: **{'yes' if exact_environment else 'no'}**",
        "",
        "This is a descriptive comparison. A percentage difference is not a confidence interval, and three cluster trials are too few to claim statistical equivalence.",
        "",
        "## Profile check",
        "",
        "| Field | Published | Candidate | Match |",
        "|---|---:|---:|---|",
    ]
    for item in checks:
        lines.append(
            f"| {item['field']} | {item['reference']} | {item['candidate']} | "
            f"{'yes' if item['matches'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## Metric differences",
            "",
            "| Engine | Metric | Published | Candidate | Change |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in metric_rows:
        lines.append(
            f"| {row['engine']} | {row['metric']} | {row['reference']:.3f} | "
            f"{row['candidate']:.3f} | {fmt_change(row['percent_change'])} |"
        )
    lines.extend(
        [
            "",
            "## Fresh-cluster HTTP p95 ranges",
            "",
            "| Engine | Published trials (ms) | Candidate trials (ms) | Published range | Candidate range |",
            "|---|---|---|---:|---:|",
        ]
    )
    for row in trial_rows:
        ref = ", ".join(f"{value:.2f}" for value in row["reference"])
        new = ", ".join(f"{value:.2f}" for value in row["candidate"])
        lines.append(
            f"| {row['engine']} | {ref} | {new} | "
            f"{row['reference_min']:.2f}-{row['reference_max']:.2f} | "
            f"{row['candidate_min']:.2f}-{row['candidate_max']:.2f} |"
        )
    output.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
