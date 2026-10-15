#!/usr/bin/env python3
"""Constant-rate Kubernetes admission load generator using only stdlib HTTP."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import csv
import http.client
import json
import math
import os
import ssl
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


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
        "min": min(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p50": percentile(values, 0.50),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


class KubernetesClient:
    def __init__(self, kubeconfig: str, timeout: float):
        env = os.environ.copy()
        env["KUBECONFIG"] = kubeconfig
        raw = subprocess.check_output(
            ["kubectl", "config", "view", "--raw", "--minify", "-o", "json"],
            env=env,
            text=True,
        )
        config = json.loads(raw)
        cluster = config["clusters"][0]["cluster"]
        user = config["users"][0]["user"]
        server = urlparse(cluster["server"])
        if server.scheme != "https":
            raise ValueError(f"only HTTPS kubeconfig servers are supported: {cluster['server']}")
        self.host = server.hostname
        self.port = server.port or 443
        self.timeout = timeout
        self.thread_local = threading.local()
        self.credential_dir = tempfile.TemporaryDirectory(prefix="policy-bench-kube-")
        credential_path = Path(self.credential_dir.name)

        ca_path = credential_path / "ca.crt"
        cert_path = credential_path / "client.crt"
        key_path = credential_path / "client.key"
        ca_path.write_bytes(base64.b64decode(cluster["certificate-authority-data"]))
        cert_path.write_bytes(base64.b64decode(user["client-certificate-data"]))
        key_path.write_bytes(base64.b64decode(user["client-key-data"]))
        os.chmod(key_path, 0o600)

        self.context = ssl.create_default_context(cafile=str(ca_path))
        self.context.load_cert_chain(str(cert_path), str(key_path))

    def _connection(self) -> http.client.HTTPSConnection:
        connection = getattr(self.thread_local, "connection", None)
        if connection is None:
            connection = http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=self.timeout,
                context=self.context,
            )
            self.thread_local.connection = connection
        return connection

    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict, str]:
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        connection = self._connection()
        try:
            connection.request(
                method,
                path,
                body=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "policy-bench/1.0",
                },
            )
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
            try:
                decoded = json.loads(response_body)
            except json.JSONDecodeError:
                decoded = {}
            return response.status, decoded, response_body
        except Exception:
            connection.close()
            self.thread_local.connection = None
            raise

    def create_pod(
        self, namespace: str, pod: dict, *, dry_run: bool = True
    ) -> tuple[int, dict, str]:
        query = "?fieldManager=policy-bench"
        if dry_run:
            query += "&dryRun=All"
        return self._request("POST", f"/api/v1/namespaces/{namespace}/pods{query}", pod)

    def replace_pod(self, namespace: str, pod: dict) -> tuple[int, dict, str]:
        name = pod["metadata"]["name"]
        return self._request(
            "PUT",
            f"/api/v1/namespaces/{namespace}/pods/{name}?dryRun=All&fieldManager=policy-bench",
            pod,
        )

    def get_pod(self, namespace: str, name: str) -> tuple[int, dict, str]:
        return self._request("GET", f"/api/v1/namespaces/{namespace}/pods/{name}")

    def delete_pod(self, namespace: str, name: str) -> tuple[int, dict, str]:
        return self._request("DELETE", f"/api/v1/namespaces/{namespace}/pods/{name}")


# Annotation used to grow the admitted object. It is inert with respect to the
# contract: no arm matches on it, mutates it, or reads it. Real clusters carry
# payloads of exactly this shape, for example
# kubectl.kubernetes.io/last-applied-configuration.
PADDING_ANNOTATION = "policy-bench-padding"


def pod_object(
    name: str,
    *,
    subject: bool = True,
    team: str | None = "platform",
    injected: str | None = None,
    padding_bytes: int = 0,
    profile: str = "minimal",
) -> dict:
    labels = {"policy-bench-subject": "true"} if subject else {"control": "true"}
    annotations: dict[str, str] = {}
    if team is not None:
        annotations["policy-bench-team"] = team
    if injected is not None:
        annotations["policy-bench-injected"] = injected
    # Zero padding leaves the object byte-identical to the published profile.
    if padding_bytes > 0:
        annotations[PADDING_ANNOTATION] = "x" * padding_bytes
    metadata: dict[str, object] = {"name": name, "labels": labels}
    if annotations:
        metadata["annotations"] = annotations
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": metadata,
        "spec": pod_spec(profile),
    }


# The published spec: one pause container and nothing else. An admitted object of
# this shape is about 300 bytes serialized, which is far below anything a workload
# controller produces.
MINIMAL_SPEC: dict = {
    "automountServiceAccountToken": False,
    "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.10"}],
    "restartPolicy": "Never",
}


def realistic_spec() -> dict:
    """A Pod spec of the shape a Deployment produces for an ordinary service.

    Nothing here is chosen for its size. It is the set of fields a normal
    workload carries: two containers, resource requests and limits, both probes,
    a PSS-restricted security context, a projected service-account token, config
    and secret mounts, and environment variables. The resulting object size is
    whatever that adds up to, and the report states it.
    """

    def container(name: str, image: str, port: int) -> dict:
        return {
            "name": name,
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "ports": [{"name": "http", "containerPort": port, "protocol": "TCP"}],
            "env": [
                {"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
                {"name": "LOG_LEVEL", "value": "info"},
                {"name": "UPSTREAM_URL", "value": "http://backend.policy-bench.svc.cluster.local:8080"},
            ],
            "resources": {
                "requests": {"cpu": "100m", "memory": "128Mi"},
                "limits": {"cpu": "500m", "memory": "512Mi"},
            },
            "livenessProbe": {
                "httpGet": {"path": "/healthz", "port": "http"},
                "initialDelaySeconds": 10,
                "periodSeconds": 10,
                "timeoutSeconds": 1,
                "failureThreshold": 3,
            },
            "readinessProbe": {
                "httpGet": {"path": "/readyz", "port": "http"},
                "initialDelaySeconds": 5,
                "periodSeconds": 5,
                "timeoutSeconds": 1,
                "failureThreshold": 3,
            },
            "volumeMounts": [
                {"name": "config", "mountPath": "/etc/app", "readOnly": True},
                {"name": "secrets", "mountPath": "/etc/app/secrets", "readOnly": True},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
        }

    return {
        "serviceAccountName": "policy-bench-app",
        "automountServiceAccountToken": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 30,
        "dnsPolicy": "ClusterFirst",
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 1000,
            "fsGroup": 2000,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            container("app", "registry.k8s.io/pause:3.10", 8080),
            container("sidecar", "registry.k8s.io/pause:3.10", 9090),
        ],
        "volumes": [
            {"name": "config", "configMap": {"name": "policy-bench-config", "defaultMode": 420}},
            {"name": "secrets", "secret": {"secretName": "policy-bench-secret", "defaultMode": 420}},
            {"name": "tmp", "emptyDir": {}},
        ],
        "affinity": {
            "podAntiAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "podAffinityTerm": {
                            "topologyKey": "kubernetes.io/hostname",
                            "labelSelector": {"matchLabels": {"app": "policy-bench-app"}},
                        },
                    }
                ]
            }
        },
        "tolerations": [
            {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
        ],
    }


def pod_spec(profile: str) -> dict:
    if profile == "minimal":
        return copy.deepcopy(MINIMAL_SPEC)
    if profile == "realistic":
        return realistic_spec()
    raise SystemExit("unknown payload profile: %s" % profile)


def verify_case(
    client: KubernetesClient,
    engine: str,
    case_name: str,
    namespace: str,
    pod: dict,
    expect_admitted: bool,
    expect_injected: str | None,
) -> dict:
    started = time.perf_counter()
    try:
        status, response, raw = client.create_pod(namespace, pod)
        transport_error = ""
    except Exception as exc:  # verification output needs the exact failing case
        status, response, raw = 0, {}, ""
        transport_error = repr(exc)
    elapsed_ms = (time.perf_counter() - started) * 1000
    admitted = 200 <= status < 300
    actual_injected = (
        response.get("metadata", {}).get("annotations", {}).get("policy-bench-injected")
        if admitted
        else None
    )
    passed = admitted == expect_admitted
    if expect_admitted:
        passed = passed and actual_injected == expect_injected
    elif engine != "baseline":
        message = response.get("message", raw)
        normalized_message = " ".join(message.split())
        passed = passed and "policy-bench-team annotation must equal platform" in normalized_message
    return {
        "case": case_name,
        "status": status,
        "admitted": admitted,
        "expected_admitted": expect_admitted,
        "injected": actual_injected,
        "expected_injected": expect_injected,
        "elapsed_ms": elapsed_ms,
        "transport_error": transport_error,
        "passed": passed,
        "response_message": response.get("message", "")[:500],
    }


def verify_update_case(
    client: KubernetesClient,
    engine: str,
    case_name: str,
    namespace: str,
    pod: dict,
    expect_admitted: bool,
    expect_injected: str | None,
) -> dict:
    started = time.perf_counter()
    try:
        status, response, raw = client.replace_pod(namespace, pod)
        transport_error = ""
    except Exception as exc:
        status, response, raw = 0, {}, ""
        transport_error = repr(exc)
    elapsed_ms = (time.perf_counter() - started) * 1000
    admitted = 200 <= status < 300
    actual_injected = (
        response.get("metadata", {}).get("annotations", {}).get("policy-bench-injected")
        if admitted
        else None
    )
    passed = admitted == expect_admitted
    if expect_admitted:
        passed = passed and actual_injected == expect_injected
    elif engine != "baseline":
        message = response.get("message", raw)
        passed = passed and "policy-bench-team annotation must equal platform" in " ".join(
            message.split()
        )
    return {
        "case": case_name,
        "status": status,
        "admitted": admitted,
        "expected_admitted": expect_admitted,
        "injected": actual_injected,
        "expected_injected": expect_injected,
        "elapsed_ms": elapsed_ms,
        "transport_error": transport_error,
        "passed": passed,
        "response_message": response.get("message", "")[:500],
    }


# The UPDATE probes read the stored Pod and write it back. A 409 means the object
# changed in between, which is an optimistic-concurrency artifact rather than a
# policy outcome, so the probe is retried against a fresh read before it counts
# as a failure.
UPDATE_CONFLICT_RETRIES = 4


def verify_update_case_with_reread(
    client: KubernetesClient,
    engine: str,
    case_name: str,
    namespace: str,
    name: str,
    build_payload,
    expect_admitted: bool,
    expect_injected,
) -> dict:
    result = None
    for attempt in range(UPDATE_CONFLICT_RETRIES):
        get_status, stored, get_raw = client.get_pod(namespace, name)
        if get_status != 200:
            raise RuntimeError(f"update seed GET returned {get_status}: {get_raw[:500]}")
        payload = build_payload(copy.deepcopy(stored))
        expected = expect_injected(payload) if callable(expect_injected) else expect_injected
        result = verify_update_case(
            client, engine, case_name, namespace, payload, expect_admitted, expected
        )
        if result["status"] != 409:
            return result
        result["conflict_retries"] = attempt + 1
    return result


def case_expectations(engine: str, policy_mode: str) -> dict[str, tuple[bool, str | None]]:
    """Expected (admitted, injected annotation) per verification case.

    Admission is decided by the validating half and the annotation by the
    mutating half, so a run installing only one of them expects a different
    outcome from a run installing both. Separated out from the case list because
    getting one cell of this table wrong silently turns the semantic gate into
    either a false failure or a rubber stamp.
    """
    mutating = engine != "baseline" and policy_mode in ("both", "mutate")
    validating = engine != "baseline" and policy_mode in ("both", "validate")
    # What a matched, admitted Pod carries once the mutating half has run.
    injected = "true" if mutating else None
    # An invalid Pod is rejected only when the validating half is installed; if it
    # is admitted instead, the mutating half still applies to it.
    invalid_admitted = not validating
    invalid_injected = injected if invalid_admitted else None
    return {
        "matched-valid": (True, injected),
        "matched-missing-team": (invalid_admitted, invalid_injected),
        "matched-wrong-team": (invalid_admitted, invalid_injected),
        # Not matched by either half, so untouched in every mode.
        "unmatched-label": (True, None),
        "unmatched-namespace": (True, None),
        # Already annotated: the contract forbids overwriting it.
        "no-overwrite": (True, "false"),
    }


def run_verify(args: argparse.Namespace, client: KubernetesClient) -> int:
    # The contract has two halves and a run may install only one of them, so the
    # expected outcome of a case depends on which half is present, not on whether
    # the arm has any policy at all.
    mutation_enabled = args.engine != "baseline" and args.policy_mode in ("both", "mutate")
    validation_enabled = args.engine != "baseline" and args.policy_mode in ("both", "validate")
    padding = args.payload_padding_bytes
    profile = args.payload_profile
    cases = []
    for iteration in range(args.iterations):
        suffix = f"{iteration}-{uuid.uuid4().hex[:8]}"
        expectations = case_expectations(args.engine, args.policy_mode)
        pods = {
            "matched-valid": ("policy-bench", pod_object(f"pb-valid-{suffix}", padding_bytes=padding, profile=profile)),
            "matched-missing-team": ("policy-bench", pod_object(f"pb-missing-{suffix}", team=None, padding_bytes=padding, profile=profile)),
            "matched-wrong-team": ("policy-bench", pod_object(f"pb-wrong-{suffix}", team="other", padding_bytes=padding, profile=profile)),
            "unmatched-label": ("policy-bench", pod_object(f"pb-label-{suffix}", subject=False, team=None, padding_bytes=padding, profile=profile)),
            "unmatched-namespace": ("policy-bench-control", pod_object(f"pb-namespace-{suffix}", team=None, padding_bytes=padding, profile=profile)),
            "no-overwrite": ("policy-bench", pod_object(f"pb-overwrite-{suffix}", injected="false", padding_bytes=padding, profile=profile)),
        }
        definitions = [
            (case_name, namespace, pod, *expectations[case_name])
            for case_name, (namespace, pod) in pods.items()
        ]
        for case_name, namespace, pod, admitted, injected in definitions:
            cases.append(
                verify_case(client, args.engine, case_name, namespace, pod, admitted, injected)
            )

        update_name = f"pb-update-{suffix}"
        update_seed = pod_object(update_name, padding_bytes=padding, profile=profile)
        # An explicit nonexistent node bypasses scheduler writes, keeping the
        # resourceVersion stable while dry-run UPDATE behavior is probed.
        update_seed["spec"]["nodeName"] = "policy-bench-no-such-node"
        try:
            create_status, stored, create_raw = client.create_pod(
                "policy-bench", update_seed, dry_run=False
            )
            if not 200 <= create_status < 300:
                raise RuntimeError(f"update seed returned {create_status}: {create_raw[:500]}")

            def drop_injected(payload: dict) -> dict:
                payload["metadata"].setdefault("annotations", {}).pop(
                    "policy-bench-injected", None
                )
                return payload

            def set_wrong_team(payload: dict) -> dict:
                payload["metadata"].setdefault("annotations", {})[
                    "policy-bench-team"
                ] = "other"
                return payload

            def set_injected_false(payload: dict) -> dict:
                payload["metadata"].setdefault("annotations", {})[
                    "policy-bench-injected"
                ] = "false"
                return payload

            cases.append(
                verify_update_case_with_reread(
                    client,
                    args.engine,
                    "update-matched-valid",
                    "policy-bench",
                    update_name,
                    drop_injected,
                    True,
                    "true" if mutation_enabled else None,
                )
            )

            cases.append(
                verify_update_case_with_reread(
                    client,
                    args.engine,
                    "update-wrong-team",
                    "policy-bench",
                    update_name,
                    set_wrong_team,
                    not validation_enabled,
                    lambda payload: None
                    if validation_enabled
                    else payload["metadata"]["annotations"].get("policy-bench-injected"),
                )
            )

            cases.append(
                verify_update_case_with_reread(
                    client,
                    args.engine,
                    "update-no-overwrite",
                    "policy-bench",
                    update_name,
                    set_injected_false,
                    True,
                    "false",
                )
            )
        except Exception as exc:
            cases.append(
                {
                    "case": "update-seed",
                    "status": 0,
                    "admitted": False,
                    "expected_admitted": True,
                    "injected": None,
                    "expected_injected": "true" if mutation_enabled else None,
                    "elapsed_ms": 0,
                    "transport_error": repr(exc),
                    "passed": False,
                    "response_message": "",
                }
            )
        finally:
            try:
                client.delete_pod("policy-bench", update_name)
            except Exception:
                pass
    document = {
        "engine": args.engine,
        "iterations": args.iterations,
        "cases": cases,
        "passed": all(case["passed"] for case in cases),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n")
    if not document["passed"]:
        for case in cases:
            if not case["passed"]:
                print(json.dumps(case), flush=True)
        return 1
    return 0


def request_once(
    client: KubernetesClient,
    engine: str,
    index: int,
    scheduled: float,
    run_token: str,
    padding_bytes: int = 0,
    profile: str = "minimal",
    expect_mutation: bool = True,
) -> dict:
    actual_start = time.perf_counter()
    pod = pod_object(
        f"pb-{engine[:3]}-{run_token}-{index}",
        padding_bytes=padding_bytes,
        profile=profile,
    )
    try:
        status, response, raw = client.create_pod("policy-bench", pod)
        transport_error = ""
    except Exception as exc:
        status, response, raw = 0, {}, ""
        transport_error = repr(exc)
    ended = time.perf_counter()
    annotations = response.get("metadata", {}).get("annotations", {})
    mutated = annotations.get("policy-bench-injected") == "true"
    admitted = 200 <= status < 300
    # A validate-only run installs no mutating policy, so a missing annotation is
    # the correct result rather than a failed request.
    ok = admitted and (not expect_mutation or mutated)
    error_text = transport_error or ("" if admitted else response.get("message", raw)[:500])
    if admitted and not ok:
        error_text = "admitted response did not contain expected mutation"
    return {
        "index": index,
        "scheduled": scheduled,
        "actual_start": actual_start,
        "ended": ended,
        "queue_delay_ms": (actual_start - scheduled) * 1000,
        "http_latency_ms": (ended - actual_start) * 1000,
        "scheduled_latency_ms": (ended - scheduled) * 1000,
        "status": status,
        "ok": ok,
        "mutated": mutated,
        "error": error_text,
    }


def run_load(args: argparse.Namespace, client: KubernetesClient) -> int:
    planned = int(round(args.duration * args.rate))
    if planned <= 0:
        raise ValueError("duration * rate must produce at least one request")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_token = uuid.uuid4().hex[:10]
    started_utc = datetime.now(timezone.utc).isoformat()
    schedule_start = time.perf_counter() + 0.5
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        for index in range(planned):
            scheduled = schedule_start + (index / args.rate)
            remaining = scheduled - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            futures.append(
                executor.submit(
                    request_once,
                    client,
                    args.engine,
                    index,
                    scheduled,
                    run_token,
                    args.payload_padding_bytes,
                    args.payload_profile,
                    args.engine != "baseline" and args.policy_mode in ("both", "mutate"),
                )
            )
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    completed_at = time.perf_counter()
    results.sort(key=lambda item: item["index"])

    if not args.discard_details:
        with (output_dir / "requests.csv").open("w", newline="") as handle:
            fieldnames = [
                "index",
                "scheduled_offset_s",
                "queue_delay_ms",
                "http_latency_ms",
                "scheduled_latency_ms",
                "status",
                "ok",
                "mutated",
                "error",
            ]
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for result in results:
                writer.writerow(
                    {
                        "index": result["index"],
                        "scheduled_offset_s": result["scheduled"] - schedule_start,
                        "queue_delay_ms": result["queue_delay_ms"],
                        "http_latency_ms": result["http_latency_ms"],
                        "scheduled_latency_ms": result["scheduled_latency_ms"],
                        "status": result["status"],
                        "ok": result["ok"],
                        "mutated": result["mutated"],
                        "error": result["error"],
                    }
                )

    http_latencies = [item["http_latency_ms"] for item in results]
    scheduled_latencies = [item["scheduled_latency_ms"] for item in results]
    queue_delays = [item["queue_delay_ms"] for item in results]
    success_count = sum(item["ok"] for item in results)
    status_counts: dict[str, int] = {}
    for item in results:
        key = str(item["status"])
        status_counts[key] = status_counts.get(key, 0) + 1
    summary = {
        "engine": args.engine,
        "started_utc": started_utc,
        "configured_duration_seconds": args.duration,
        "target_rps": args.rate,
        "concurrency": args.concurrency,
        "payload_padding_bytes": args.payload_padding_bytes,
        "payload_profile": args.payload_profile,
        "policy_mode": args.policy_mode,
        "payload_bytes": len(json.dumps(pod_object("pb-size-probe", padding_bytes=args.payload_padding_bytes, profile=args.payload_profile))),
        "request_timeout_seconds": args.timeout,
        "planned_requests": planned,
        "completed_requests": len(results),
        "successful_requests": success_count,
        "failed_requests": len(results) - success_count,
        "status_counts": status_counts,
        "offer_window_seconds": (planned - 1) / args.rate if planned > 1 else 0,
        "wall_seconds_including_drain": completed_at - schedule_start,
        "achieved_success_rps_including_drain": success_count / max(completed_at - schedule_start, 1e-9),
        "http_latency_ms": distribution(http_latencies),
        "scheduled_latency_ms": distribution(scheduled_latencies),
        "client_queue_delay_ms": distribution(queue_delays),
        "passed": len(results) == planned and success_count == planned,
    }
    (output_dir / "latency-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["passed"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--engine", choices=("baseline", "kyverno", "kyverno-cel", "gatekeeper", "native"), required=True)
    parser.add_argument("--timeout", type=float, default=30)
    # Bytes of inert annotation added to every Pod this client sends. 0 keeps the
    # published payload. Both the measured requests and the semantic probes use
    # it, so the contract is verified at the size being measured.
    parser.add_argument("--payload-padding-bytes", type=int, default=0)
    # Shape of the Pod the client sends. "minimal" is the published one-container
    # Pod; "realistic" is the shape a Deployment produces for an ordinary service.
    parser.add_argument("--payload-profile", choices=("minimal", "realistic"), default="minimal")
    parser.add_argument("--policy-mode", choices=("both", "validate", "mutate"), default="both")
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--iterations", type=int, default=20)
    verify.add_argument("--output", required=True)

    load = subparsers.add_parser("load")
    load.add_argument("--duration", type=float, required=True)
    load.add_argument("--rate", type=float, required=True)
    load.add_argument("--concurrency", type=int, required=True)
    load.add_argument("--output-dir", required=True)
    load.add_argument("--discard-details", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = KubernetesClient(args.kubeconfig, args.timeout)
    if args.command == "verify":
        return run_verify(args, client)
    return run_load(args, client)


if __name__ == "__main__":
    raise SystemExit(main())
