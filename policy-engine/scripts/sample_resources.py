#!/usr/bin/env python3
"""Sample total kind-node and per-Kubernetes-container CPU/memory."""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def docker_json(socket_path: str, path: str) -> dict:
    connection = UnixHTTPConnection(socket_path)
    connection.request("GET", path)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    if response.status >= 300:
        raise RuntimeError(f"Docker API {path} returned {response.status}: {payload[:300]!r}")
    return json.loads(payload)


def value_at(document: dict, *keys: str, default: int = 0) -> int:
    current = document
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    try:
        return int(current)
    except (TypeError, ValueError):
        return default


def node_stats(socket_path: str, node: str) -> dict:
    stats = docker_json(
        socket_path,
        f"/containers/{quote(node, safe='')}/stats?stream=false&one-shot=true",
    )
    usage = value_at(stats, "memory_stats", "usage")
    inactive = value_at(stats, "memory_stats", "stats", "inactive_file")
    if inactive == 0:
        inactive = value_at(stats, "memory_stats", "stats", "total_inactive_file")
    return {
        "cpu_total_nanoseconds": value_at(stats, "cpu_stats", "cpu_usage", "total_usage"),
        "cpu_system_nanoseconds": value_at(stats, "cpu_stats", "system_cpu_usage"),
        "online_cpus": value_at(stats, "cpu_stats", "online_cpus"),
        "cpu_periods": value_at(stats, "cpu_stats", "throttling_data", "periods"),
        "cpu_throttled_periods": value_at(
            stats, "cpu_stats", "throttling_data", "throttled_periods"
        ),
        "cpu_throttled_nanoseconds": value_at(
            stats, "cpu_stats", "throttling_data", "throttled_time"
        ),
        "memory_working_set_bytes": max(0, usage - inactive),
        "memory_usage_bytes": usage,
    }


def container_stats(node: str) -> list[dict]:
    raw = subprocess.check_output(
        ["docker", "exec", node, "crictl", "stats", "-o", "json"],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    document = json.loads(raw)
    output = []
    for item in document.get("stats", []):
        attributes = item.get("attributes", {})
        labels = attributes.get("labels", {})
        output.append(
            {
                "namespace": labels.get("io.kubernetes.pod.namespace", ""),
                "pod": labels.get("io.kubernetes.pod.name", ""),
                "container": attributes.get("metadata", {}).get("name", ""),
                "cpu_cores": value_at(item, "cpu", "usageNanoCores", "value") / 1_000_000_000,
                "cpu_core_seconds": value_at(item, "cpu", "usageCoreNanoSeconds", "value") / 1_000_000_000,
                "memory_working_set_bytes": value_at(item, "memory", "workingSetBytes", "value"),
                "memory_usage_bytes": value_at(item, "memory", "usageBytes", "value"),
            }
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--phase", choices=("idle", "load"), required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--output", required=True)
    parser.add_argument("--docker-socket", default="/var/run/docker.sock")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    deadline = started + args.duration
    sample_index = 0
    previous_node_cpu: tuple[int, float] | None = None
    with output_path.open("w") as handle:
        while time.perf_counter() < deadline:
            sample_due = started + sample_index * args.interval
            if sample_due >= deadline:
                break
            remaining = sample_due - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            try:
                sampled_at = time.perf_counter()
                node = node_stats(args.docker_socket, args.node)
                cpu_total = node.pop("cpu_total_nanoseconds")
                if previous_node_cpu is None:
                    node["cpu_cores"] = None
                else:
                    previous_total, previous_time = previous_node_cpu
                    elapsed = sampled_at - previous_time
                    node["cpu_cores"] = max(0, cpu_total - previous_total) / 1_000_000_000 / elapsed
                previous_node_cpu = (cpu_total, sampled_at)
                record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "offset_seconds": time.perf_counter() - started,
                    "engine": args.engine,
                    "phase": args.phase,
                    "node": node,
                    "containers": container_stats(args.node),
                    "error": "",
                }
            except Exception as exc:
                record = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "offset_seconds": time.perf_counter() - started,
                    "engine": args.engine,
                    "phase": args.phase,
                    "node": {},
                    "containers": [],
                    "error": repr(exc),
                }
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            sample_index += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
