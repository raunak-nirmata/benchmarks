#!/usr/bin/env python3
"""Validate invariants, pool trials, and render the benchmark report."""

from __future__ import annotations

import csv
import json
import math
import re
import shlex
import statistics
import sys
from pathlib import Path


ENGINES = ("baseline", "kyverno", "gatekeeper", "native")


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def resource_category(engine: str, container: dict) -> str | None:
    if container.get("container") == "kube-apiserver":
        return "apiserver"
    if engine == "kyverno" and container.get("namespace") == "kyverno":
        return "engine"
    if engine == "gatekeeper" and container.get("namespace") == "gatekeeper-system":
        return "engine"
    return None


def read_resources(path: Path, engine: str) -> dict[str, dict]:
    series: dict[str, dict[str, list[float]]] = {
        "node": {"cpu": [], "memory": []},
        "apiserver": {"cpu": [], "memory": []},
        "engine": {"cpu": [], "memory": []},
    }
    lines = path.read_text().splitlines()
    if not lines:
        raise SystemExit(f"resource file is empty: {path}")
    for line_number, line in enumerate(lines, 1):
        item = json.loads(line)
        if item.get("error"):
            raise SystemExit(f"resource sampler error in {path}:{line_number}: {item['error']}")
        node = item.get("node", {})
        if "memory_working_set_bytes" not in node:
            raise SystemExit(f"node resource sample missing in {path}:{line_number}")
        if node.get("cpu_cores") is not None:
            series["node"]["cpu"].append(float(node["cpu_cores"]))
        series["node"]["memory"].append(float(node.get("memory_working_set_bytes", 0)))
        aggregate = {
            "apiserver": {"cpu": 0.0, "memory": 0.0},
            "engine": {"cpu": 0.0, "memory": 0.0},
        }
        for container in item.get("containers", []):
            category = resource_category(engine, container)
            if category:
                aggregate[category]["cpu"] += float(container.get("cpu_cores", 0))
                aggregate[category]["memory"] += float(container.get("memory_working_set_bytes", 0))
        for category in ("apiserver", "engine"):
            series[category]["cpu"].append(aggregate[category]["cpu"])
            series[category]["memory"].append(aggregate[category]["memory"])
    return {
        category: {
            "cpu_cores": distribution(values["cpu"]),
            "memory_bytes": distribution(values["memory"]),
        }
        for category, values in series.items()
    }


def fmt(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def pct_delta(value: float | None, baseline: float | None) -> str:
    if value is None or baseline in (None, 0):
        return "n/a"
    return f"{((value / baseline) - 1) * 100:+.1f}%"


def mib(value: float | None) -> float | None:
    return None if value is None else value / 1024 / 1024


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: aggregate.py RUN_DIR")
    run_dir = Path(sys.argv[1]).resolve()
    trial_dirs = sorted(path for path in run_dir.glob("trial-*") if path.is_dir())
    if not trial_dirs:
        raise SystemExit("no trial directories found")
    effective_config = {}
    for line in (run_dir / "effective-config.env").read_text().splitlines():
        key, separator, raw_value = line.partition("=")
        if separator:
            decoded = shlex.split(raw_value)
            effective_config[key] = decoded[0] if decoded else ""
    expected_trial_count = int(effective_config["TRIALS"])
    expected_trial_names = [f"trial-{index}" for index in range(1, expected_trial_count + 1)]
    if [path.name for path in trial_dirs] != expected_trial_names:
        raise SystemExit(
            f"trial set mismatch: found {[path.name for path in trial_dirs]}, expected {expected_trial_names}"
        )
    host_text = (trial_dirs[0] / "baseline" / "host.txt").read_text()

    def host_value(pattern: str) -> str:
        match = re.search(pattern, host_text, re.MULTILINE)
        return match.group(1).strip() if match else "unknown"

    execution_host = {
        "operating_system": host_value(r"^\s*Operating System:\s*(.+)$"),
        "architecture": host_value(r"^\s*Architecture:\s*(.+)$"),
        "cpu_count": host_value(r"^\s*CPUs:\s*(.+)$"),
        "cpu_model": host_value(r"^\s*Model name:\s*(.+)$"),
        "total_memory": host_value(r"^\s*Total Memory:\s*(.+)$"),
        "docker_client_version": host_value(r"^\s*Version:\s*(.+)$"),
        "docker_server_version": host_value(r"^\s*Server Version:\s*(.+)$"),
    }

    pooled: dict[str, dict] = {
        engine: {
            "http": [],
            "scheduled": [],
            "queue": [],
            "trials": [],
            "resource_idle": [],
            "resource_load": [],
        }
        for engine in ENGINES
    }
    invariant_rows = []
    expected_load_shape = None
    expected_checksums = None
    expected_node_runtime = None
    cluster_uids: set[str] = set()
    expected_kubernetes_version = None
    expected_host_fingerprint = None
    expected_images: dict[str, tuple] = {}

    for trial_dir in trial_dirs:
        for engine in ENGINES:
            engine_dir = trial_dir / engine
            if not engine_dir.is_dir():
                raise SystemExit(f"missing benchmark cell: {engine_dir}")
            equivalence = load_json(engine_dir / "equivalence.json")
            equivalence_after = load_json(engine_dir / "equivalence-after.json")
            isolation = load_json(engine_dir / "isolation.json")
            isolation_after = load_json(engine_dir / "isolation-after.json")
            latency = load_json(engine_dir / "latency-summary.json")
            cluster_identity = load_json(engine_dir / "cluster-identity.json")
            cluster_uid = cluster_identity.get("metadata", {}).get("uid")
            if not cluster_uid or cluster_uid in cluster_uids:
                raise SystemExit(f"missing or reused cluster UID in {engine_dir}: {cluster_uid}")
            cluster_uids.add(cluster_uid)
            kubernetes_version = load_json(engine_dir / "kubernetes-version.json").get(
                "serverVersion", {}
            ).get("gitVersion")
            if expected_kubernetes_version is None:
                expected_kubernetes_version = kubernetes_version
            if kubernetes_version != expected_kubernetes_version:
                raise SystemExit(f"Kubernetes server version drift in {engine_dir}")
            if kubernetes_version != effective_config["KUBECTL_VERSION"]:
                raise SystemExit(
                    f"Kubernetes server version {kubernetes_version} does not match pinned "
                    f"version {effective_config['KUBECTL_VERSION']} in {engine_dir}"
                )
            cell_host = (engine_dir / "host.txt").read_text()
            cell_fingerprint = tuple(
                re.search(pattern, cell_host, re.MULTILINE).group(1).strip()
                if re.search(pattern, cell_host, re.MULTILINE)
                else "unknown"
                for pattern in (
                    r"^\s*Operating System:\s*(.+)$",
                    r"^\s*Architecture:\s*(.+)$",
                    r"^\s*CPUs:\s*(.+)$",
                    r"^\s*Model name:\s*(.+)$",
                    r"^\s*Server Version:\s*(.+)$",
                )
            )
            if expected_host_fingerprint is None:
                expected_host_fingerprint = cell_fingerprint
            if cell_fingerprint != expected_host_fingerprint:
                raise SystemExit(f"host fingerprint drift in {engine_dir}")
            image_document = load_json(engine_dir / "container-images.json")
            image_inventory = tuple(
                sorted(
                    (
                        item.get("id", ""),
                        tuple(sorted(item.get("repoDigests") or [])),
                        tuple(sorted(item.get("repoTags") or [])),
                    )
                    for item in image_document.get("images", [])
                )
            )
            if engine not in expected_images:
                expected_images[engine] = image_inventory
            if image_inventory != expected_images[engine]:
                raise SystemExit(f"resolved container image drift for {engine} in {engine_dir}")
            node_runtime = load_json(engine_dir / "node-runtime.json")
            if not node_runtime["passed"]:
                raise SystemExit(f"node runtime gate failed in {engine_dir}")
            if expected_node_runtime is None:
                expected_node_runtime = node_runtime["runtime"]
            if node_runtime["runtime"] != expected_node_runtime:
                raise SystemExit(f"node runtime configuration drift detected in {engine_dir}")
            checksum_lines = (engine_dir / "input-checksums.txt").read_text().splitlines()
            # Every cluster records the full shared configuration and policy
            # corpus. Any mid-run edit invalidates the comparison.
            config_checksums = tuple(checksum_lines)
            if expected_checksums is None:
                expected_checksums = config_checksums
            if config_checksums != expected_checksums:
                raise SystemExit(f"input configuration drift detected in {engine_dir}")
            load_shape = (
                latency["configured_duration_seconds"],
                latency["target_rps"],
                latency["concurrency"],
                latency["planned_requests"],
            )
            if expected_load_shape is None:
                expected_load_shape = load_shape
            if load_shape != expected_load_shape:
                raise SystemExit(f"load shape drift detected in {engine_dir}: {load_shape}")
            passed = (
                equivalence["passed"]
                and equivalence_after["passed"]
                and isolation["passed"]
                and isolation_after["passed"]
                and latency["passed"]
            )
            if not passed:
                raise SystemExit(f"invalid run cannot be reported: {engine_dir}")
            invariant_rows.append(
                {
                    "trial": trial_dir.name,
                    "engine": engine,
                    "equivalence": equivalence["passed"],
                    "semantic_cases": f"{sum(case['passed'] for case in equivalence['cases'])}/{len(equivalence['cases'])}",
                    "isolation": isolation["passed"],
                    "requests": f"{latency['successful_requests']}/{latency['planned_requests']}",
                }
            )
            with (engine_dir / "requests.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
                if len(rows) != latency["planned_requests"]:
                    raise SystemExit(f"raw request count mismatch in {engine_dir}")
                for expected_index, row in enumerate(rows):
                    if int(row["index"]) != expected_index:
                        raise SystemExit(f"request index gap in {engine_dir} at {expected_index}")
                    if row["ok"] != "True" or int(row["status"]) != 201:
                        raise SystemExit(f"failed raw request in {engine_dir}: {row}")
                    expected_mutated = "False" if engine == "baseline" else "True"
                    if row["mutated"] != expected_mutated:
                        raise SystemExit(f"mutation mismatch in {engine_dir}: {row}")
                    queue_value = float(row["queue_delay_ms"])
                    http_value = float(row["http_latency_ms"])
                    scheduled_value = float(row["scheduled_latency_ms"])
                    if not math.isclose(
                        scheduled_value, queue_value + http_value, rel_tol=0, abs_tol=0.001
                    ):
                        raise SystemExit(f"latency accounting mismatch in {engine_dir}: {row}")
                    pooled[engine]["http"].append(float(row["http_latency_ms"]))
                    pooled[engine]["scheduled"].append(float(row["scheduled_latency_ms"]))
                    pooled[engine]["queue"].append(float(row["queue_delay_ms"]))
            pooled[engine]["trials"].append(latency)
            pooled[engine]["resource_idle"].append(read_resources(engine_dir / "resources-idle.jsonl", engine))
            pooled[engine]["resource_load"].append(read_resources(engine_dir / "resources-load.jsonl", engine))

    missing = [engine for engine in ENGINES if not pooled[engine]["trials"]]
    if missing:
        raise SystemExit(f"missing engine results: {', '.join(missing)}")

    summary: dict[str, dict] = {}
    for engine, data in pooled.items():
        engine_summary = {
            "trial_count": len(data["trials"]),
            "successful_requests": sum(item["successful_requests"] for item in data["trials"]),
            "trial_runs": [
                {
                    "successful_requests": item["successful_requests"],
                    "failed_requests": item["failed_requests"],
                    "wall_seconds_including_drain": item["wall_seconds_including_drain"],
                    "achieved_success_rps_including_drain": item[
                        "achieved_success_rps_including_drain"
                    ],
                    "http_latency_ms": item["http_latency_ms"],
                    "scheduled_latency_ms": item["scheduled_latency_ms"],
                    "client_queue_delay_ms": item["client_queue_delay_ms"],
                }
                for item in data["trials"]
            ],
            "http_latency_ms": distribution(data["http"]),
            "scheduled_latency_ms": distribution(data["scheduled"]),
            "client_queue_delay_ms": distribution(data["queue"]),
            "achieved_rps": distribution(
                [item["achieved_success_rps_including_drain"] for item in data["trials"]]
            ),
            "resources": {},
        }
        for category in ("node", "apiserver", "engine"):
            engine_summary["resources"][category] = {}
            for metric in ("cpu_cores", "memory_bytes"):
                idle_means = [item[category][metric]["mean"] for item in data["resource_idle"]]
                load_means = [item[category][metric]["mean"] for item in data["resource_load"]]
                load_p95s = [item[category][metric]["p95"] for item in data["resource_load"]]
                idle_mean = statistics.fmean(idle_means)
                load_mean = statistics.fmean(load_means)
                engine_summary["resources"][category][metric] = {
                    "idle_mean": idle_mean,
                    "load_mean": load_mean,
                    "load_p95_mean_across_trials": statistics.fmean(load_p95s),
                    "load_minus_idle": load_mean - idle_mean,
                }
        summary[engine] = engine_summary

    result = {
        "run_directory": str(run_dir),
        "effective_config": effective_config,
        "execution_host": execution_host,
        "load_shape": {
            "duration_seconds": expected_load_shape[0],
            "target_rps": expected_load_shape[1],
            "concurrency": expected_load_shape[2],
            "requests_per_engine_per_trial": expected_load_shape[3],
        },
        "invariants": invariant_rows,
        "engines": summary,
    }
    (run_dir / "summary.json").write_text(json.dumps(result, indent=2) + "\n")

    baseline_latency = summary["baseline"]["http_latency_ms"]["p95"]
    baseline_cpu = summary["baseline"]["resources"]["node"]["cpu_cores"]["load_mean"]
    lines = [
        "# Admission policy engine benchmark",
        "",
        "All figures in this report are measured outputs from this run. The baseline is the same pinned Kubernetes cluster without a policy engine.",
        "",
        "## Execution environment",
        "",
        f"- Host: {execution_host['operating_system']}, {execution_host['architecture']}",
        f"- CPU: {execution_host['cpu_count']} x {execution_host['cpu_model']}",
        f"- Host memory: {execution_host['total_memory']}",
        f"- Docker Engine: {execution_host['docker_server_version']}",
        "- Topology: one single-control-plane kind node per isolated engine/trial",
        "",
        "## Test shape",
        "",
        f"- Trials: {len(trial_dirs)} per engine",
        f"- Offered load: {expected_load_shape[1]} requests/s for {expected_load_shape[0]} seconds",
        f"- Exact requests: {expected_load_shape[3]} per engine per trial",
        f"- Maximum client concurrency: {expected_load_shape[2]}",
        f"- Kubernetes node image: `{effective_config['KIND_NODE_IMAGE']}`",
        f"- Kyverno chart: {effective_config['KYVERNO_CHART_VERSION']}",
        f"- Gatekeeper chart: {effective_config['GATEKEEPER_CHART_VERSION']}",
        "- Policy operation scope: Pod CREATE and UPDATE; semantic probes exercise both",
        "- Request: dry-run CREATE of the same labelled Pod, with valid team annotation and mutation absent",
        "",
        "## End-to-end admission latency",
        "",
        "| Engine | Successful requests | p50 ms | p95 ms | p99 ms | max ms | p95 vs baseline | Queue p95 ms | Achieved RPS |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for engine in ENGINES:
        item = summary[engine]
        latency = item["http_latency_ms"]
        lines.append(
            f"| {engine} | {item['successful_requests']} | {fmt(latency['p50'])} | {fmt(latency['p95'])} | {fmt(latency['p99'])} | {fmt(latency['max'])} | {pct_delta(latency['p95'], baseline_latency)} | {fmt(item['client_queue_delay_ms']['p95'])} | {fmt(item['achieved_rps']['mean'])} |"
        )

    lines.extend(
        [
            "",
            "HTTP latency starts when a worker begins the API call. Queue latency is reported separately; scheduled latency in `summary.json` combines both and is the overload-sensitive measure.",
            "",
            "## Per-trial latency and throughput",
            "",
            "| Trial | Engine | HTTP p50 ms | HTTP p95 ms | HTTP p99 ms | Scheduled p95 ms | Queue p95 ms | Wall incl. drain s | Achieved RPS | Failed |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for trial_index in range(len(trial_dirs)):
        for engine in ENGINES:
            item = summary[engine]["trial_runs"][trial_index]
            http = item["http_latency_ms"]
            lines.append(
                f"| {trial_index + 1} | {engine} | {fmt(http['p50'])} | {fmt(http['p95'])} | {fmt(http['p99'])} | {fmt(item['scheduled_latency_ms']['p95'])} | {fmt(item['client_queue_delay_ms']['p95'])} | {fmt(item['wall_seconds_including_drain'])} | {fmt(item['achieved_success_rps_including_drain'])} | {item['failed_requests']} |"
            )

    lines.extend(
        [
            "",
            "## Total cluster-node resources",
            "",
            "| Engine | CPU mean cores | CPU p95 cores | CPU over baseline | CPU load-idle | Memory mean MiB | Memory p95 MiB | Memory load-idle MiB |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for engine in ENGINES:
        node = summary[engine]["resources"]["node"]
        cpu = node["cpu_cores"]
        memory = node["memory_bytes"]
        lines.append(
            f"| {engine} | {fmt(cpu['load_mean'], 3)} | {fmt(cpu['load_p95_mean_across_trials'], 3)} | {pct_delta(cpu['load_mean'], baseline_cpu)} | {fmt(cpu['load_minus_idle'], 3)} | {fmt(mib(memory['load_mean']))} | {fmt(mib(memory['load_p95_mean_across_trials']))} | {fmt(mib(memory['load_minus_idle']))} |"
        )

    lines.extend(
        [
            "",
            "## Admission data-path containers",
            "",
            "| Engine | Measured component | CPU mean cores | CPU p95 cores | Memory mean MiB | Memory p95 MiB |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for engine in ("kyverno", "gatekeeper"):
        resource = summary[engine]["resources"]["engine"]
        lines.append(
            f"| {engine} | all containers in engine namespace | {fmt(resource['cpu_cores']['load_mean'], 3)} | {fmt(resource['cpu_cores']['load_p95_mean_across_trials'], 3)} | {fmt(mib(resource['memory_bytes']['load_mean']))} | {fmt(mib(resource['memory_bytes']['load_p95_mean_across_trials']))} |"
        )
    native_resource = summary["native"]["resources"]["apiserver"]
    lines.append(
        f"| native | kube-apiserver (policy is in-process; not separable) | {fmt(native_resource['cpu_cores']['load_mean'], 3)} | {fmt(native_resource['cpu_cores']['load_p95_mean_across_trials'], 3)} | {fmt(mib(native_resource['memory_bytes']['load_mean']))} | {fmt(mib(native_resource['memory_bytes']['load_p95_mean_across_trials']))} |"
    )

    lines.extend(
        [
            "",
            "## Validity gates",
            "",
            "| Trial | Engine | Semantic cases | Equivalence | Isolation | Successful/planned |",
            "|---|---|---:|---|---|---:|",
        ]
    )
    for row in invariant_rows:
        lines.append(
            f"| {row['trial']} | {row['engine']} | {row['semantic_cases']} | {'pass' if row['equivalence'] else 'fail'} | {'pass' if row['isolation'] else 'fail'} | {row['requests']} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation limits",
            "",
            "- This is an admission data-path benchmark on kind, not a production-cluster sizing promise.",
            "- The native policy executes inside kube-apiserver, so its policy-only CPU and memory cannot be separated. Total node deltas are the fairest cross-engine resource comparison.",
            "- Webhook engines pay local TLS and AdmissionReview serialization costs; native CEL does not. That architectural cost is intentionally included.",
            "- Dry-run removes scheduler, image pull, workload startup, and object cleanup noise. It also does not measure background scanning or audit controllers, which are disabled.",
            "- The measured workload combines mutation and validation and contains matched, valid Pod CREATE requests only. UPDATE and rejection behavior are correctness probes, not performance workloads.",
            "- Gatekeeper used its chart-generated fail-open webhooks (Ignore; 1 s mutation and 3 s validation timeouts), while Kyverno used Fail/10 s and native policies used Fail. No measured request failed, but failure semantics were not normalized.",
            "- These are three 60-second fresh-cluster replicates on one shared four-core host. Treat the cluster trial, not each request, as the experimental unit.",
            "- The published client metric starts when the worker begins preparing the request and ends after reading the response; it includes small client-side Pod construction and JSON serialization costs.",
            "- Warm-up and measurement use separate client processes, so the measured process starts with a new connection pool.",
            "- The resource window is the 60-second offer period plus one second; it may not include a longer overload drain.",
            "- Review `host.txt`, raw request CSV files, resource JSONL, input hashes, and API-server metric snapshots before comparing this run to another machine.",
            "",
        ]
    )
    (run_dir / "REPORT.md").write_text("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
