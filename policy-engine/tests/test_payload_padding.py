from __future__ import annotations

import importlib.util
import json
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


class PayloadPaddingTests(unittest.TestCase):
    """Padding grows the admitted object without touching the contract."""

    def test_default_pod_is_unpadded(self):
        pod = loadgen.pod_object("pb-1")
        self.assertNotIn(loadgen.PADDING_ANNOTATION, pod["metadata"]["annotations"])
        self.assertEqual(pod["metadata"]["annotations"], {"policy-bench-team": "platform"})

    def test_padding_grows_the_object_by_about_the_requested_bytes(self):
        base = len(json.dumps(loadgen.pod_object("pb-1")))
        for size in (1024, 8192, 65536):
            grown = len(json.dumps(loadgen.pod_object("pb-1", padding_bytes=size)))
            self.assertGreaterEqual(grown - base, size)
            # only the annotation key, quoting and separator are overhead
            self.assertLess(grown - base, size + 128)

    def test_padding_leaves_the_contract_fields_alone(self):
        padded = loadgen.pod_object("pb-1", padding_bytes=4096)
        plain = loadgen.pod_object("pb-1")
        self.assertEqual(padded["spec"], plain["spec"])
        self.assertEqual(padded["metadata"]["labels"], plain["metadata"]["labels"])
        self.assertEqual(
            padded["metadata"]["annotations"]["policy-bench-team"],
            plain["metadata"]["annotations"]["policy-bench-team"],
        )

    def test_padding_preserves_every_contract_case(self):
        cases = (
            {"subject": False, "team": None},
            {"team": None},
            {"team": "other"},
            {"injected": "false"},
        )
        for case in cases:
            plain = loadgen.pod_object("pb-1", **case)
            padded = loadgen.pod_object("pb-1", padding_bytes=2048, **case)
            plain_annotations = dict(plain["metadata"].get("annotations", {}))
            padded_annotations = dict(padded["metadata"].get("annotations", {}))
            padded_annotations.pop(loadgen.PADDING_ANNOTATION, None)
            self.assertEqual(plain_annotations, padded_annotations, case)
            self.assertEqual(plain["metadata"]["labels"], padded["metadata"]["labels"], case)

    def test_zero_padding_is_byte_identical(self):
        self.assertEqual(
            json.dumps(loadgen.pod_object("pb-1"), sort_keys=True),
            json.dumps(loadgen.pod_object("pb-1", padding_bytes=0), sort_keys=True),
        )


class PayloadProfileTests(unittest.TestCase):
    """The realistic profile must be an ordinary workload Pod, not disguised padding."""

    def test_minimal_profile_is_the_published_spec(self):
        pod = loadgen.pod_object("pb-1")
        self.assertEqual(
            pod["spec"],
            {
                "automountServiceAccountToken": False,
                "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.10"}],
                "restartPolicy": "Never",
            },
        )
        self.assertEqual(pod["spec"], loadgen.pod_object("pb-1", profile="minimal")["spec"])

    def test_realistic_profile_carries_ordinary_workload_fields(self):
        spec = loadgen.pod_object("pb-1", profile="realistic")["spec"]
        self.assertEqual(len(spec["containers"]), 2)
        for container in spec["containers"]:
            self.assertIn("requests", container["resources"])
            self.assertIn("limits", container["resources"])
            self.assertIn("livenessProbe", container)
            self.assertIn("readinessProbe", container)
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(container["securityContext"]["runAsNonRoot"])
        self.assertEqual(len(spec["volumes"]), 3)
        self.assertIn("serviceAccountName", spec)

    def test_realistic_profile_has_no_filler_field(self):
        """Size must come from real fields, so no repeated-character value."""
        text = json.dumps(loadgen.pod_object("pb-1", profile="realistic"))
        self.assertNotIn(loadgen.PADDING_ANNOTATION, text)
        self.assertNotIn("xxxxxxxxxx", text)

    def test_realistic_profile_size_is_in_a_workload_like_range(self):
        size = len(json.dumps(loadgen.pod_object("pb-1", profile="realistic")))
        # A Deployment-managed Pod lands in single-digit KB. Outside this range the
        # fixture has either lost its fields or grown into padding.
        self.assertGreater(size, 2000)
        self.assertLess(size, 8000)

    def test_realistic_profile_preserves_the_contract(self):
        pod = loadgen.pod_object("pb-1", profile="realistic")
        self.assertEqual(pod["metadata"]["labels"], {"policy-bench-subject": "true"})
        self.assertEqual(pod["metadata"]["annotations"], {"policy-bench-team": "platform"})
        self.assertNotIn("policy-bench-injected", pod["metadata"]["annotations"])
        unmatched = loadgen.pod_object("pb-1", subject=False, team=None, profile="realistic")
        self.assertNotIn("policy-bench-subject", unmatched["metadata"]["labels"])

    def test_profile_and_padding_compose(self):
        base = len(json.dumps(loadgen.pod_object("pb-1", profile="realistic")))
        padded = len(json.dumps(loadgen.pod_object("pb-1", profile="realistic", padding_bytes=4096)))
        self.assertGreaterEqual(padded - base, 4096)

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(SystemExit):
            loadgen.pod_object("pb-1", profile="enormous")


if __name__ == "__main__":
    unittest.main()
