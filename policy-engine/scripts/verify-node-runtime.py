#!/usr/bin/env python3
"""Record and verify that the kind node has no Docker CPU quota."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    raw = json.loads(Path(args.inspect).read_text())
    if not isinstance(raw, list) or len(raw) != 1:
        raise SystemExit("Docker inspect output must contain exactly one node")
    host = raw[0].get("HostConfig", {})
    runtime = {
        "nano_cpus": int(host.get("NanoCpus") or 0),
        "cpu_period": int(host.get("CpuPeriod") or 0),
        "cpu_quota": int(host.get("CpuQuota") or 0),
        "cpu_shares": int(host.get("CpuShares") or 0),
        "cpuset_cpus": host.get("CpusetCpus") or "",
        "memory_limit_bytes": int(host.get("Memory") or 0),
    }
    failures = []
    if runtime["nano_cpus"] > 0:
        failures.append(f"NanoCpus is limited: {runtime['nano_cpus']}")
    if runtime["cpu_quota"] > 0:
        failures.append(
            f"CPU CFS quota is enabled: quota={runtime['cpu_quota']} "
            f"period={runtime['cpu_period']}"
        )
    document = {"passed": not failures, "failures": failures, "runtime": runtime}
    Path(args.output).write_text(json.dumps(document, indent=2) + "\n")
    if failures:
        print("; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
