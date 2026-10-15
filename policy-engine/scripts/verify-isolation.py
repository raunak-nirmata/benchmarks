#!/usr/bin/env python3
"""Verify cell isolation, expected policy inventory, readiness, and CPU settings."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any


ENGINES = ("baseline", "kyverno", "kyverno-cel", "gatekeeper", "native")
GATEKEEPER_CONSTRAINT_RESOURCE = (
    "policybenchrequireteam.constraints.gatekeeper.sh"
)
# The Kyverno chart creates the same set of webhook configurations whether the
# policies are legacy ClusterPolicy or CEL Validating/MutatingPolicy, so the
# kyverno-cel arm shares the kyverno expected set.
_KYVERNO_WEBHOOK_CONFIGS = {
    "kyverno-policy-mutating-webhook-cfg",
    "kyverno-resource-mutating-webhook-cfg",
    "kyverno-verify-mutating-webhook-cfg",
    "kyverno-cel-exception-validating-webhook-cfg",
    "kyverno-exception-validating-webhook-cfg",
    "kyverno-global-context-validating-webhook-cfg",
    "kyverno-policy-validating-webhook-cfg",
    "kyverno-resource-validating-webhook-cfg",
}
EXPECTED_WEBHOOK_CONFIGS = {
    "baseline": set(),
    "native": set(),
    "gatekeeper": {
        "gatekeeper-mutating-webhook-configuration",
        "gatekeeper-validating-webhook-configuration",
    },
    "kyverno": set(_KYVERNO_WEBHOOK_CONFIGS),
    "kyverno-cel": set(_KYVERNO_WEBHOOK_CONFIGS),
}


def kubectl(
    kubeconfig: str, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig
    return subprocess.run(
        ["kubectl", *args],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def get_json(kubeconfig: str, resource: str, *extra: str) -> dict[str, Any]:
    result = kubectl(kubeconfig, "get", resource, *extra, "-o", "json", check=False)
    if result.returncode != 0:
        return {"items": []}
    return json.loads(result.stdout)


def names(document: dict[str, Any]) -> set[str]:
    return {
        item.get("metadata", {}).get("name", "")
        for item in document.get("items", [])
        if item.get("metadata", {}).get("name")
    }


def webhook_matches_pods(webhook: dict[str, Any]) -> bool:
    for rule in webhook.get("rules", []):
        resources = rule.get("resources", [])
        if "*" in resources or "pods" in resources:
            return True
    return False


def expected_pod_webhook_count(engine: str, policy_mode: str) -> int:
    """How many Pod data-path webhooks this cell should have.

    Kyverno builds its webhooks from the policies that are installed, so a run
    carrying one half of the contract produces one Pod webhook rather than two.
    Gatekeeper's webhooks come from its chart and match every resource, so both
    exist whatever the policies are.
    """
    if engine in ("kyverno", "kyverno-cel") and policy_mode != "both":
        return 1
    return 2


def expected_policy_names(rendered_path: str | None) -> tuple[set[str], set[str]]:
    """Mutate and validate policy names this cell is expected to have installed.

    Falls back to the single published pair when no manifest is given, so the
    script still runs standalone against a POLICY_COUNT=1 cluster.
    """
    if not rendered_path:
        return {"policy-bench-mutate"}, {"policy-bench-validate"}
    manifest = json.loads(Path(rendered_path).read_text())
    installed = manifest.get("names", [])
    mutate = {name for name in installed if name.startswith("policy-bench-mutate")}
    validate = {name for name in installed if name.startswith("policy-bench-validate")}
    return mutate, validate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--engine", choices=ENGINES, required=True)
    parser.add_argument("--output", required=True)
    # The rendered manifest written by render-policies.py. It is the authority on
    # which policy objects this cell installed, so the inventory check scales
    # with POLICY_COUNT instead of assuming one pair per arm.
    parser.add_argument("--rendered")
    parser.add_argument("--policy-mode", choices=("both", "validate", "mutate"), default="both")
    args = parser.parse_args()
    expected_mutate, expected_validate = expected_policy_names(args.rendered)

    failures: list[str] = []
    namespace_names = names(get_json(args.kubeconfig, "namespaces"))
    kyverno_present = "kyverno" in namespace_names
    gatekeeper_present = "gatekeeper-system" in namespace_names
    if kyverno_present != (args.engine in ("kyverno", "kyverno-cel")):
        failures.append(f"kyverno namespace presence={kyverno_present}")
    if gatekeeper_present != (args.engine == "gatekeeper"):
        failures.append(f"gatekeeper namespace presence={gatekeeper_present}")

    native_documents = {
        "native_mutating_policies": get_json(args.kubeconfig, "mutatingadmissionpolicies"),
        "native_mutating_bindings": get_json(args.kubeconfig, "mutatingadmissionpolicybindings"),
        "native_validating_policies": get_json(args.kubeconfig, "validatingadmissionpolicies"),
        "native_validating_bindings": get_json(args.kubeconfig, "validatingadmissionpolicybindings"),
    }
    inventory = {
        "native_mutating_policies": names(native_documents["native_mutating_policies"]),
        "native_mutating_bindings": names(native_documents["native_mutating_bindings"]),
        "native_validating_policies": names(native_documents["native_validating_policies"]),
        "native_validating_bindings": names(native_documents["native_validating_bindings"]),
        "kyverno_policies": names(get_json(args.kubeconfig, "clusterpolicies.kyverno.io")),
        "kyverno_cel_mutating_policies": names(
            get_json(args.kubeconfig, "mutatingpolicies.policies.kyverno.io")
        ),
        "kyverno_cel_validating_policies": names(
            get_json(args.kubeconfig, "validatingpolicies.policies.kyverno.io")
        ),
        "gatekeeper_templates": names(
            get_json(args.kubeconfig, "constrainttemplates.templates.gatekeeper.sh")
        ),
        "gatekeeper_constraints": names(
            get_json(
                args.kubeconfig,
                GATEKEEPER_CONSTRAINT_RESOURCE,
            )
        ),
        "gatekeeper_mutators": names(
            get_json(args.kubeconfig, "assignmetadata.mutations.gatekeeper.sh")
        ),
    }
    expected_inventory = {
        "native_mutating_policies": expected_mutate if args.engine == "native" else set(),
        "native_mutating_bindings": expected_mutate if args.engine == "native" else set(),
        "native_validating_policies": expected_validate if args.engine == "native" else set(),
        "native_validating_bindings": expected_validate if args.engine == "native" else set(),
        "kyverno_policies": expected_mutate | expected_validate
        if args.engine == "kyverno"
        else set(),
        "kyverno_cel_mutating_policies": expected_mutate
        if args.engine == "kyverno-cel"
        else set(),
        "kyverno_cel_validating_policies": expected_validate
        if args.engine == "kyverno-cel"
        else set(),
        # One ConstraintTemplate defines the constraint kind at any policy count.
        # It exists only to be instantiated by the Constraints, which are the
        # validating half, so a mutate-only run installs neither.
        "gatekeeper_templates": {"policybenchrequireteam"}
        if args.engine == "gatekeeper" and expected_validate
        else set(),
        "gatekeeper_constraints": expected_validate if args.engine == "gatekeeper" else set(),
        "gatekeeper_mutators": expected_mutate if args.engine == "gatekeeper" else set(),
    }
    for category, expected in expected_inventory.items():
        if inventory[category] != expected:
            failures.append(
                f"{category}={sorted(inventory[category])}, expected={sorted(expected)}"
            )
    if args.engine == "native":
        for category in ("native_mutating_policies", "native_validating_policies"):
            # Every copy is checked: at a higher policy count one divergent copy
            # would otherwise go unnoticed.
            for item in native_documents[category].get("items", []):
                policy_name = item.get("metadata", {}).get("name", "")
                if item.get("spec", {}).get("failurePolicy") != "Fail":
                    failures.append(f"native failurePolicy is not Fail: {policy_name}")
                operations = {
                    operation
                    for rule in item.get("spec", {})
                    .get("matchConstraints", {})
                    .get("resourceRules", [])
                    for operation in rule.get("operations", [])
                }
                if operations != {"CREATE", "UPDATE"}:
                    failures.append(
                        f"native operation scope mismatch: {policy_name}={operations}"
                    )
        for binding in native_documents["native_validating_bindings"].get("items", []):
            if binding.get("spec", {}).get("validationActions") != ["Deny"]:
                failures.append(
                    "native validation binding action is not exactly Deny: "
                    f"{binding.get('metadata', {}).get('name', '')}"
                )

    webhook_documents = [
        get_json(args.kubeconfig, "mutatingwebhookconfigurations"),
        get_json(args.kubeconfig, "validatingwebhookconfigurations"),
    ]
    webhook_configs: set[str] = set()
    all_webhook_configs: set[str] = set()
    pod_webhooks: list[dict[str, Any]] = []
    for document in webhook_documents:
        for item in document.get("items", []):
            config_name = item.get("metadata", {}).get("name", "")
            if config_name:
                all_webhook_configs.add(config_name)
            if config_name.startswith("kyverno-") or config_name.startswith("gatekeeper-"):
                webhook_configs.add(config_name)
            for webhook in item.get("webhooks", []):
                if webhook_matches_pods(webhook):
                    operations = sorted(
                        {
                            operation
                            for rule in webhook.get("rules", [])
                            for operation in rule.get("operations", [])
                        }
                    )
                    pod_webhooks.append(
                        {
                            "configuration": config_name,
                            "name": webhook.get("name"),
                            "operations": operations,
                            "failure_policy": webhook.get("failurePolicy"),
                            "timeout_seconds": webhook.get("timeoutSeconds"),
                            "reinvocation_policy": webhook.get("reinvocationPolicy"),
                        }
                    )
    if webhook_configs != EXPECTED_WEBHOOK_CONFIGS[args.engine]:
        failures.append(
            f"third-party webhook configurations={sorted(webhook_configs)}, "
            f"expected={sorted(EXPECTED_WEBHOOK_CONFIGS[args.engine])}"
        )
    if all_webhook_configs != EXPECTED_WEBHOOK_CONFIGS[args.engine]:
        failures.append(
            f"all webhook configurations={sorted(all_webhook_configs)}, "
            f"expected={sorted(EXPECTED_WEBHOOK_CONFIGS[args.engine])}"
        )

    relevant = [
        item
        for item in pod_webhooks
        if item["configuration"] in EXPECTED_WEBHOOK_CONFIGS[args.engine]
    ]
    expected_pod_webhooks = expected_pod_webhook_count(args.engine, args.policy_mode)
    if args.engine in ("kyverno", "gatekeeper"):
        if len(relevant) != expected_pod_webhooks:
            failures.append(
                f"expected {expected_pod_webhooks} Pod data-path webhook(s), "
                f"found {len(relevant)}"
            )
        for item in relevant:
            if not {"CREATE", "UPDATE"}.issubset(item["operations"]):
                failures.append(f"webhook operation mismatch: {item}")
    expected_failure = {"kyverno": "Fail", "gatekeeper": "Ignore"}.get(args.engine)
    if expected_failure:
        for item in relevant:
            if item["failure_policy"] != expected_failure:
                failures.append(f"webhook failure policy mismatch: {item}")
    expected_webhook_details = {
        "kyverno-resource-mutating-webhook-cfg": (10, "IfNeeded"),
        "kyverno-resource-validating-webhook-cfg": (10, None),
        "gatekeeper-mutating-webhook-configuration": (1, "Never"),
        "gatekeeper-validating-webhook-configuration": (3, None),
    }
    if args.engine in ("kyverno", "gatekeeper"):
        for item in relevant:
            expected_detail = expected_webhook_details.get(item["configuration"])
            if expected_detail is None:
                failures.append(f"unexpected Pod data-path webhook: {item}")
                continue
            expected_timeout, expected_reinvocation = expected_detail
            if item["timeout_seconds"] != expected_timeout:
                failures.append(f"webhook timeout mismatch: {item}")
            if item["reinvocation_policy"] != expected_reinvocation:
                failures.append(f"webhook reinvocation mismatch: {item}")

    pods = get_json(args.kubeconfig, "pods", "--all-namespaces")
    engine_namespace = {
        "kyverno": "kyverno",
        "kyverno-cel": "kyverno",
        "gatekeeper": "gatekeeper-system",
    }.get(args.engine)
    engine_container_resources: list[dict[str, Any]] = []
    if engine_namespace:
        running_engine_pods = 0
        for pod in pods.get("items", []):
            if pod.get("metadata", {}).get("namespace") != engine_namespace:
                continue
            if pod.get("status", {}).get("phase") == "Running":
                running_engine_pods += 1
            for container in pod.get("spec", {}).get("containers", []):
                resources = container.get("resources", {})
                record = {
                    "pod": pod.get("metadata", {}).get("name"),
                    "container": container.get("name"),
                    "resources": resources,
                }
                engine_container_resources.append(record)
                if resources.get("limits") or resources.get("requests"):
                    failures.append(f"unexpected engine CPU/memory requests or limits: {record}")
        if running_engine_pods < 1:
            failures.append(f"no running Pods in {engine_namespace}")

    deployments = get_json(args.kubeconfig, "deployments", "--all-namespaces")
    expected_deployment = {
        "kyverno": ("kyverno", "kyverno-admission-controller"),
        "kyverno-cel": ("kyverno", "kyverno-admission-controller"),
        "gatekeeper": ("gatekeeper-system", "gatekeeper-controller-manager"),
    }.get(args.engine)
    if expected_deployment:
        matching = [
            item
            for item in deployments.get("items", [])
            if (
                item.get("metadata", {}).get("namespace"),
                item.get("metadata", {}).get("name"),
            )
            == expected_deployment
        ]
        if len(matching) != 1:
            failures.append(f"expected deployment missing: {expected_deployment}")
        else:
            deployment = matching[0]
            if deployment.get("spec", {}).get("replicas") != 1:
                failures.append(f"admission replica count is not one: {expected_deployment}")
            if deployment.get("status", {}).get("readyReplicas") != 1:
                failures.append(f"admission deployment is not ready: {expected_deployment}")

    document = {
        "engine": args.engine,
        "passed": not failures,
        "failures": failures,
        "namespaces": sorted(namespace_names),
        "policy_inventory": {key: sorted(value) for key, value in inventory.items()},
        "third_party_webhook_configurations": sorted(webhook_configs),
        "all_webhook_configurations": sorted(all_webhook_configs),
        "pod_matching_webhooks": pod_webhooks,
        "engine_container_resources": engine_container_resources,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n")
    if failures:
        print("; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
