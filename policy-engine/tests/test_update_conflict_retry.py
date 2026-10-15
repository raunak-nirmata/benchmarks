from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loadgen = load_module("benchmark_loadgen", ROOT / "scripts" / "loadgen.py")

STORED = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {
        "name": "pb-update-1",
        "resourceVersion": "100",
        "annotations": {"policy-bench-team": "platform", "policy-bench-injected": "true"},
    },
    "spec": {"containers": []},
}
CONFLICT = {
    "message": 'Operation cannot be fulfilled on pods "pb-update-1": the object has '
    "been modified; please apply your changes to the latest version and try again"
}


class StubClient:
    """Replays a scripted sequence of replace_pod outcomes."""

    def __init__(self, statuses: list[int]):
        self.statuses = list(statuses)
        self.gets = 0
        self.replaces = 0

    def get_pod(self, namespace: str, name: str):
        self.gets += 1
        return 200, STORED, ""

    def replace_pod(self, namespace: str, pod: dict):
        self.replaces += 1
        status = self.statuses.pop(0)
        if status == 409:
            return 409, CONFLICT, ""
        return status, pod, ""


def run(statuses, expect_injected="false"):
    client = StubClient(statuses)

    def build(payload):
        payload["metadata"]["annotations"]["policy-bench-injected"] = "false"
        return payload

    result = loadgen.verify_update_case_with_reread(
        client, "kyverno-cel", "update-no-overwrite", "policy-bench",
        "pb-update-1", build, True, expect_injected,
    )
    return client, result


class UpdateConflictRetry(unittest.TestCase):
    def test_succeeds_without_retry_when_there_is_no_conflict(self):
        client, result = run([200])
        self.assertTrue(result["passed"])
        self.assertEqual(client.replaces, 1)
        self.assertEqual(client.gets, 1)
        self.assertNotIn("conflict_retries", result)

    def test_retries_a_conflict_against_a_fresh_read(self):
        client, result = run([409, 409, 200])
        self.assertTrue(result["passed"], result)
        self.assertEqual(client.replaces, 3)
        # Each retry must re-read, otherwise it would resend a stale
        # resourceVersion and conflict forever.
        self.assertEqual(client.gets, 3)

    def test_gives_up_after_the_retry_budget_and_reports_the_conflict(self):
        statuses = [409] * loadgen.UPDATE_CONFLICT_RETRIES
        client, result = run(statuses)
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], 409)
        self.assertEqual(client.replaces, loadgen.UPDATE_CONFLICT_RETRIES)
        self.assertEqual(result["conflict_retries"], loadgen.UPDATE_CONFLICT_RETRIES)

    def test_a_real_denial_is_not_retried(self):
        # 400 is a policy outcome and must be reported as-is, not retried away.
        client, result = run([400], expect_injected=None)
        self.assertEqual(client.replaces, 1)
        self.assertEqual(result["status"], 400)


if __name__ == "__main__":
    unittest.main()
