from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = load_module("benchmark_compare_modes", ROOT / "scripts" / "compare-modes.py")


def summary(engine_cpu: float, *, policy_count="16", rps="200", tag="after") -> dict:
    return {
        "policy_count": int(policy_count),
        "payload_bytes": 317,
        "kyverno_cel_variant": "selectors",
        "effective_config": {
            "ENGINES": "baseline kyverno-cel",
            "POLICY_COUNT": policy_count,
            "KYVERNO_CEL_VARIANT": "selectors",
            "PAYLOAD_PROFILE": "minimal",
            "PAYLOAD_PADDING_BYTES": "0",
            "TARGET_RPS": rps,
            "DURATION_SECONDS": "60",
            "TRIALS": "3",
            "KIND_NODE_IMAGE": "kindest/node:v1.36.1",
            "KYVERNO_IMAGE_TAG": tag,
            "GATEKEEPER_CHART_VERSION": "3.23.0",
        },
        "engines": {
            "kyverno-cel": {
                "trial_runs": [
                    {"http_latency_ms": {"p50": 2.0, "p95": 3.0, "p99": 4.0}}
                ],
                "resources": {
                    "node": {"cpu_cores": {"load_mean": 0.9}},
                    "apiserver": {"cpu_cores": {"load_mean": 0.3}},
                    "engine": {
                        "cpu_cores": {"load_mean": engine_cpu},
                        "memory_bytes": {"load_mean": 70e6},
                    },
                },
            }
        },
    }


def run_compare(root: Path) -> None:
    import sys

    argv = sys.argv
    sys.argv = ["compare-modes.py", str(root)]
    try:
        compare.main()
    finally:
        sys.argv = argv


def sweep(**modes) -> Path:
    root = Path(tempfile.mkdtemp())
    for mode, doc in modes.items():
        (root / mode).mkdir()
        (root / mode / "summary.json").write_text(json.dumps(doc))
    return root


class CompareModes(unittest.TestCase):
    def test_writes_a_report_and_a_json_sidecar(self):
        root = sweep(validate=summary(0.17), mutate=summary(0.48))
        run_compare(root)
        self.assertTrue((root / "MODES.md").is_file())
        data = json.loads((root / "modes.json").read_text())
        self.assertTrue(data["comparable"])
        self.assertAlmostEqual(data["modes"]["mutate"]["kyverno-cel"]["engine_cpu_cores"], 0.48)

    def test_ratio_is_computed_from_engine_cpu(self):
        rows = compare.ratio_table(
            {"validate": summary(0.2), "mutate": summary(0.6)}
        )
        self.assertTrue(any("3.0x" in line for line in rows))

    def test_mismatched_policy_count_is_flagged_not_silently_divided(self):
        differing = compare.check_comparable(
            {"validate": summary(0.2, policy_count="16"),
             "mutate": summary(0.6, policy_count="8")}
        )
        self.assertTrue(any("POLICY_COUNT" in d for d in differing))

    def test_mismatched_build_is_flagged(self):
        differing = compare.check_comparable(
            {"validate": summary(0.2, tag="before"), "mutate": summary(0.6, tag="after")}
        )
        self.assertTrue(any("KYVERNO_IMAGE_TAG" in d for d in differing))

    def test_identical_configuration_is_comparable(self):
        self.assertEqual(
            compare.check_comparable({"validate": summary(0.2), "mutate": summary(0.6)}), []
        )

    def test_a_partial_sweep_still_reports(self):
        # Only the validating half completed; the report must still be written.
        root = sweep(validate=summary(0.17))
        run_compare(root)
        text = (root / "MODES.md").read_text()
        self.assertIn("Validating half only", text)
        self.assertNotIn("mutate / validate", text)

    def test_an_empty_sweep_fails_loudly(self):
        with self.assertRaises(SystemExit):
            run_compare(Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
