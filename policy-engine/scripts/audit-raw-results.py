#!/usr/bin/env python3
"""Independently audit raw benchmark evidence and compare it to frozen claims."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shlex
import statistics
from pathlib import Path
from typing import Any


ENGINES = ("baseline", "native", "gatekeeper", "kyverno")


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
    }


def parse_env(path: Path) -> dict[str, str]:
    output = {}
    for line in path.read_text().splitlines():
        key, separator, raw = line.partition("=")
        if separator:
            decoded = shlex.split(raw)
            output[key] = decoded[0] if decoded else ""
    return output


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resource_category(engine: str, container: dict[str, Any]) -> str | None:
    if container.get("container") == "kube-apiserver":
        return "apiserver"
    if engine == "kyverno" and container.get("namespace") == "kyverno":
        return "engine"
    if engine == "gatekeeper" and container.get("namespace") == "gatekeeper-system":
        return "engine"
    return None


def read_resource_series(path: Path, engine: str) -> dict[str, dict[str, list[float]]]:
    series = {
        "node": {"cpu": [], "memory": []},
        "apiserver": {"cpu": [], "memory": []},
        "engine": {"cpu": [], "memory": []},
    }
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        item = json.loads(line)
        if item.get("error"):
            raise ValueError(f"{path}:{line_number}: {item['error']}")
        node = item.get("node", {})
        if node.get("cpu_cores") is not None:
            series["node"]["cpu"].append(float(node["cpu_cores"]))
        if "memory_working_set_bytes" not in node:
            raise ValueError(f"{path}:{line_number}: missing node memory")
        series["node"]["memory"].append(float(node["memory_working_set_bytes"]))
        per_sample = {
            "apiserver": {"cpu": 0.0, "memory": 0.0},
            "engine": {"cpu": 0.0, "memory": 0.0},
        }
        for container in item.get("containers", []):
            category = resource_category(engine, container)
            if category:
                per_sample[category]["cpu"] += float(container.get("cpu_cores", 0))
                per_sample[category]["memory"] += float(
                    container.get("memory_working_set_bytes", 0)
                )
        for category in ("apiserver", "engine"):
            series[category]["cpu"].append(per_sample[category]["cpu"])
            series[category]["memory"].append(per_sample[category]["memory"])
    return series


def close(actual: float, expected: float, tolerance: float = 1e-7) -> bool:
    return math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance)


def reference_value(reference: dict[str, Any], engine: str, metric: str) -> float:
    item = reference["engines"][engine]
    paths = {
        "http_p50": ("http_latency_ms", "p50"),
        "http_p95": ("http_latency_ms", "p95"),
        "http_p99": ("http_latency_ms", "p99"),
        "scheduled_p95": ("scheduled_latency_p95_ms",),
        "queue_p95": ("queue_delay_p95_ms",),
        "node_cpu_mean": ("node", "cpu_load_mean_cores"),
        "node_cpu_p95_mean": ("node", "cpu_load_p95_mean_cores"),
        "node_cpu_delta": ("node", "cpu_load_minus_idle_cores"),
        "node_memory_mean": ("node", "memory_load_mean_bytes"),
        "node_memory_p95_mean": ("node", "memory_load_p95_mean_bytes"),
        "node_memory_delta": ("node", "memory_load_minus_idle_bytes"),
    }
    value: Any = item
    for key in paths[metric]:
        value = value[key]
    return float(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir")
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--verify-published-values", action="store_true")
    parser.add_argument("--verify-frozen-artifact-hashes", action="store_true")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    reference = json.loads(Path(args.reference).read_text())
    reported_summary = json.loads((run_dir / "summary.json").read_text())
    config = parse_env(run_dir / "effective-config.env")
    trial_count = int(config["TRIALS"])
    planned = int(round(float(config["DURATION_SECONDS"]) * float(config["TARGET_RPS"])))
    expected_semantic_cases = int(config.get("VERIFY_ITERATIONS", "20")) * 9
    rate = float(config["TARGET_RPS"])
    idle_expected = math.ceil(
        float(config["IDLE_SAMPLE_SECONDS"]) / float(config["SAMPLE_INTERVAL_SECONDS"])
    )
    load_expected = math.ceil(
        (float(config["DURATION_SECONDS"]) + 1.0)
        / float(config["SAMPLE_INTERVAL_SECONDS"])
    )

    failures: list[str] = []
    cells: list[dict[str, Any]] = []
    pooled = {
        engine: {"http": [], "scheduled": [], "queue": [], "idle": [], "load": []}
        for engine in ENGINES
    }
    checksum_corpus: tuple[str, ...] | None = None

    for trial in range(1, trial_count + 1):
        for engine in ENGINES:
            cell = run_dir / f"trial-{trial}" / engine
            if not cell.is_dir():
                failures.append(f"missing cell: {cell}")
                continue
            try:
                equivalence = json.loads((cell / "equivalence.json").read_text())
                isolation = json.loads((cell / "isolation.json").read_text())
                summary = json.loads((cell / "latency-summary.json").read_text())
                if not equivalence.get("passed"):
                    failures.append(f"semantic gate failed: {cell}")
                if len(equivalence.get("cases", [])) != expected_semantic_cases:
                    failures.append(
                        f"semantic case count is {len(equivalence.get('cases', []))}, "
                        f"expected {expected_semantic_cases}: {cell}"
                    )
                if not all(case.get("passed") for case in equivalence.get("cases", [])):
                    failures.append(f"individual semantic case failed: {cell}")
                if not isolation.get("passed"):
                    failures.append(f"isolation gate failed: {cell}")
                if "VERIFY_ITERATIONS" in config:
                    for post_name in ("equivalence-after.json", "isolation-after.json"):
                        post_path = cell / post_name
                        if not post_path.is_file():
                            failures.append(f"missing post-load gate: {post_path}")
                        elif not json.loads(post_path.read_text()).get("passed"):
                            failures.append(f"post-load gate failed: {post_path}")
                    runtime_path = cell / "node-runtime.json"
                    if not runtime_path.is_file():
                        failures.append(f"missing node runtime gate: {runtime_path}")
                    elif not json.loads(runtime_path.read_text()).get("passed"):
                        failures.append(f"node runtime gate failed: {runtime_path}")

                checksum_lines = tuple((cell / "input-checksums.txt").read_text().splitlines())
                if checksum_corpus is None:
                    checksum_corpus = checksum_lines
                elif checksum_lines != checksum_corpus:
                    failures.append(f"input checksum drift: {cell}")

                with (cell / "requests.csv").open(newline="") as handle:
                    rows = list(csv.DictReader(handle))
                if len(rows) != planned:
                    failures.append(f"request count {len(rows)} != {planned}: {cell}")
                http: list[float] = []
                scheduled: list[float] = []
                queue: list[float] = []
                reconstructed_completion = 0.0
                for expected_index, row in enumerate(rows):
                    if int(row["index"]) != expected_index:
                        failures.append(f"request index mismatch at {expected_index}: {cell}")
                        break
                    expected_offset = expected_index / rate
                    if not close(float(row["scheduled_offset_s"]), expected_offset, 1e-8):
                        failures.append(f"offered schedule mismatch at {expected_index}: {cell}")
                        break
                    if row["ok"] != "True" or int(row["status"]) != 201:
                        failures.append(f"unsuccessful request at {expected_index}: {cell}")
                        break
                    expected_mutated = "False" if engine == "baseline" else "True"
                    if row["mutated"] != expected_mutated:
                        failures.append(f"mutation mismatch at {expected_index}: {cell}")
                        break
                    q = float(row["queue_delay_ms"])
                    h = float(row["http_latency_ms"])
                    s = float(row["scheduled_latency_ms"])
                    if not close(s, q + h, 1e-6):
                        failures.append(f"latency sum mismatch at {expected_index}: {cell}")
                        break
                    http.append(h)
                    scheduled.append(s)
                    queue.append(q)
                    reconstructed_completion = max(
                        reconstructed_completion,
                        float(row["scheduled_offset_s"]) + s / 1000.0,
                    )

                computed = {
                    "http": distribution(http),
                    "scheduled": distribution(scheduled),
                    "queue": distribution(queue),
                }
                for computed_name, summary_name in (
                    ("http", "http_latency_ms"),
                    ("scheduled", "scheduled_latency_ms"),
                    ("queue", "client_queue_delay_ms"),
                ):
                    for metric in ("count", "mean", "p50", "p95", "p99", "max"):
                        if not close(
                            float(computed[computed_name][metric]),
                            float(summary[summary_name][metric]),
                        ):
                            failures.append(
                                f"stored {summary_name}.{metric} mismatch: {cell}"
                            )
                approximate_rps = planned / reconstructed_completion
                if abs(
                    approximate_rps - float(summary["achieved_success_rps_including_drain"])
                ) > 1.0:
                    failures.append(f"achieved RPS cannot be reconstructed closely: {cell}")

                idle = read_resource_series(cell / "resources-idle.jsonl", engine)
                load = read_resource_series(cell / "resources-load.jsonl", engine)
                if len(idle["node"]["memory"]) != idle_expected:
                    failures.append(f"idle resource sample count mismatch: {cell}")
                if len(load["node"]["memory"]) != load_expected:
                    failures.append(f"load resource sample count mismatch: {cell}")
                pooled[engine]["http"].extend(http)
                pooled[engine]["scheduled"].extend(scheduled)
                pooled[engine]["queue"].extend(queue)
                pooled[engine]["idle"].append(idle)
                pooled[engine]["load"].append(load)
                cells.append(
                    {
                        "trial": trial,
                        "engine": engine,
                        "requests": len(rows),
                        "semantic_cases": len(equivalence.get("cases", [])),
                        "http_p95_ms": computed["http"]["p95"],
                        "approximate_achieved_rps": approximate_rps,
                    }
                )
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                failures.append(f"audit error in {cell}: {exc}")

    recomputed: dict[str, Any] = {}
    for engine in ENGINES:
        data = pooled[engine]
        if not data["http"]:
            continue
        engine_result: dict[str, Any] = {
            "http_latency_ms": distribution(data["http"]),
            "scheduled_latency_ms": distribution(data["scheduled"]),
            "queue_delay_ms": distribution(data["queue"]),
            "node": {},
        }
        for metric, output_prefix in (("cpu", "cpu"), ("memory", "memory")):
            idle_means = [statistics.fmean(item["node"][metric]) for item in data["idle"]]
            load_means = [statistics.fmean(item["node"][metric]) for item in data["load"]]
            load_p95s = [percentile(item["node"][metric], 0.95) for item in data["load"]]
            engine_result["node"][f"{output_prefix}_load_mean"] = statistics.fmean(load_means)
            engine_result["node"][f"{output_prefix}_load_p95_mean"] = statistics.fmean(load_p95s)
            engine_result["node"][f"{output_prefix}_load_minus_idle"] = (
                statistics.fmean(load_means) - statistics.fmean(idle_means)
            )
        if engine in ("kyverno", "gatekeeper"):
            engine_result["engine_namespace"] = {
                "cpu_load_mean": statistics.fmean(
                    statistics.fmean(item["engine"]["cpu"]) for item in data["load"]
                ),
                "memory_load_mean": statistics.fmean(
                    statistics.fmean(item["engine"]["memory"]) for item in data["load"]
                ),
            }
        recomputed[engine] = engine_result

        reference_map = {
            "http_p50": engine_result["http_latency_ms"]["p50"],
            "http_p95": engine_result["http_latency_ms"]["p95"],
            "http_p99": engine_result["http_latency_ms"]["p99"],
            "scheduled_p95": engine_result["scheduled_latency_ms"]["p95"],
            "queue_p95": engine_result["queue_delay_ms"]["p95"],
            "node_cpu_mean": engine_result["node"]["cpu_load_mean"],
            "node_cpu_p95_mean": engine_result["node"]["cpu_load_p95_mean"],
            "node_cpu_delta": engine_result["node"]["cpu_load_minus_idle"],
            "node_memory_mean": engine_result["node"]["memory_load_mean"],
            "node_memory_p95_mean": engine_result["node"]["memory_load_p95_mean"],
            "node_memory_delta": engine_result["node"]["memory_load_minus_idle"],
        }
        reported_engine = reported_summary["engines"][engine]
        reported_node = reported_engine["resources"]["node"]
        reported_map = {
            "http_p50": reported_engine["http_latency_ms"]["p50"],
            "http_p95": reported_engine["http_latency_ms"]["p95"],
            "http_p99": reported_engine["http_latency_ms"]["p99"],
            "scheduled_p95": reported_engine["scheduled_latency_ms"]["p95"],
            "queue_p95": reported_engine["client_queue_delay_ms"]["p95"],
            "node_cpu_mean": reported_node["cpu_cores"]["load_mean"],
            "node_cpu_p95_mean": reported_node["cpu_cores"][
                "load_p95_mean_across_trials"
            ],
            "node_cpu_delta": reported_node["cpu_cores"]["load_minus_idle"],
            "node_memory_mean": reported_node["memory_bytes"]["load_mean"],
            "node_memory_p95_mean": reported_node["memory_bytes"][
                "load_p95_mean_across_trials"
            ],
            "node_memory_delta": reported_node["memory_bytes"]["load_minus_idle"],
        }
        for metric, actual in reference_map.items():
            if not close(float(actual), float(reported_map[metric])):
                failures.append(
                    f"aggregated summary mismatch for {engine}.{metric}: "
                    f"{actual} != {reported_map[metric]}"
                )
        if int(reported_engine["successful_requests"]) != planned * trial_count:
            failures.append(f"aggregated successful request count mismatch for {engine}")
        if engine in ("kyverno", "gatekeeper"):
            reported_engine_resource = reported_engine["resources"]["engine"]
            for metric, actual, expected in (
                (
                    "engine_cpu_mean",
                    engine_result["engine_namespace"]["cpu_load_mean"],
                    reported_engine_resource["cpu_cores"]["load_mean"],
                ),
                (
                    "engine_memory_mean",
                    engine_result["engine_namespace"]["memory_load_mean"],
                    reported_engine_resource["memory_bytes"]["load_mean"],
                ),
            ):
                if not close(float(actual), float(expected)):
                    failures.append(
                        f"aggregated summary mismatch for {engine}.{metric}: "
                        f"{actual} != {expected}"
                    )
        if args.verify_published_values:
            for metric, actual in reference_map.items():
                expected = reference_value(reference, engine, metric)
                if not close(float(actual), expected):
                    failures.append(
                        f"published reference mismatch for {engine}.{metric}: {actual} != {expected}"
                    )

    artifact_checks = {}
    for name, key in (("summary.json", "summary_sha256"), ("REPORT.md", "report_sha256")):
        actual = file_sha256(run_dir / name)
        expected = reference["artifact"][key]
        artifact_checks[name] = {"actual": actual, "expected": expected, "matches": actual == expected}
        if args.verify_frozen_artifact_hashes and actual != expected:
            failures.append(f"artifact checksum mismatch: {name}")

    result = {
        "passed": not failures,
        "failures": failures,
        "run_dir": str(run_dir),
        "cell_count": len(cells),
        "measured_request_count": sum(item["requests"] for item in cells),
        "cells": cells,
        "recomputed": recomputed,
        "artifact_checks": artifact_checks,
        "verified_published_values": args.verify_published_values,
        "enforced_frozen_artifact_hashes": args.verify_frozen_artifact_hashes,
        "notes": [
            "Percentiles use linear interpolation at (N - 1) * q.",
            "Fresh-cluster creation is established by the orchestration code; the historical result did not record per-cluster UIDs.",
            "Achieved RPS is checked approximately because requests.csv does not store the final executor timestamp.",
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "failures": failures}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
