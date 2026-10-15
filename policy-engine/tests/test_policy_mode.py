from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICIES = ROOT / "policies"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load_module("benchmark_render_policies", ROOT / "scripts" / "render-policies.py")
isolation = load_module("benchmark_verify_isolation", ROOT / "scripts" / "verify-isolation.py")

ARMS = ("kyverno", "kyverno-cel", "gatekeeper", "native")
MUTATE_KINDS = {
    "MutatingPolicy",
    "MutatingAdmissionPolicy",
    "MutatingAdmissionPolicyBinding",
    "AssignMetadata",
}
VALIDATE_KINDS = {
    "ValidatingPolicy",
    "ValidatingAdmissionPolicy",
    "ValidatingAdmissionPolicyBinding",
    "ConstraintTemplate",
    "PolicyBenchRequireTeam",
}


# Legacy Kyverno expresses both halves of the contract as ClusterPolicy, so kind
# alone cannot classify it; those cells are checked by rule type instead.
AMBIGUOUS_KINDS = {"ClusterPolicy"}


def render_arm(engine: str, mode: str, count: int = 16, variant: str = "matchconditions"):
    tmp = tempfile.mkdtemp()
    manifest = render.render(engine, count, POLICIES, Path(tmp), variant, mode)
    kinds: list[str] = []
    text = ""
    for path in sorted(Path(tmp).glob("*.yaml")):
        body = path.read_text()
        # Header comments describe both halves of the contract, so the rule-type
        # check has to look at the manifest, not at the prose above it.
        text += "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        kinds.extend(re.findall(r"^kind: (\S+)", body, re.M))
    return manifest, kinds, text


class PolicyMode(unittest.TestCase):
    def test_both_is_unchanged(self):
        for engine in ARMS:
            manifest, _, _ = render_arm(engine, "both")
            with self.subTest(engine=engine):
                self.assertEqual(manifest["policy_objects"], 32)
                self.assertEqual(manifest["policy_mode"], "both")

    def test_validate_installs_only_validating_objects(self):
        for engine in ARMS:
            manifest, kinds, text = render_arm(engine, "validate")
            with self.subTest(engine=engine):
                self.assertEqual(manifest["policy_objects"], 16)
                self.assertTrue(kinds, "rendered nothing")
                self.assertFalse(
                    set(kinds) & MUTATE_KINDS,
                    f"{engine} validate-only still rendered {set(kinds) & MUTATE_KINDS}",
                )
                self.assertTrue(set(kinds) <= VALIDATE_KINDS | AMBIGUOUS_KINDS, set(kinds))
                # Rule-level check, which is what actually matters for the arm
                # whose kind is the same on both halves.
                self.assertNotIn("mutate:", text)
                self.assertNotIn("patchStrategicMerge", text)
                self.assertFalse(
                    [n for n in manifest["names"] if n.startswith("policy-bench-mutate")]
                )

    def test_mutate_installs_only_mutating_objects(self):
        for engine in ARMS:
            manifest, kinds, text = render_arm(engine, "mutate")
            with self.subTest(engine=engine):
                self.assertEqual(manifest["policy_objects"], 16)
                self.assertTrue(kinds, "rendered nothing")
                self.assertFalse(set(kinds) & VALIDATE_KINDS)
                self.assertNotIn("validate:", text)
                self.assertFalse(
                    [n for n in manifest["names"] if n.startswith("policy-bench-validate")]
                )

    def test_gatekeeper_keeps_its_constraint_template_for_validate(self):
        # The ConstraintTemplate carries no policy name but defines the kind the
        # Constraints are instances of, so dropping it would break the arm.
        _, kinds, _ = render_arm("gatekeeper", "validate")
        self.assertIn("ConstraintTemplate", kinds)
        _, kinds, _ = render_arm("gatekeeper", "mutate")
        self.assertNotIn("ConstraintTemplate", kinds)

    def test_selectors_variant_honours_the_mode(self):
        manifest, kinds, text = render_arm("kyverno-cel", "validate", variant="selectors")
        self.assertEqual(manifest["policy_objects"], 16)
        self.assertEqual(set(kinds), {"ValidatingPolicy"})

    def test_both_variants_install_identical_validating_work(self):
        # This is the whole point of validate-only: the mutating halves of the two
        # variants are not equivalent, the validating halves are.
        counts = {}
        for variant in ("matchconditions", "selectors"):
            manifest, kinds, _ = render_arm("kyverno-cel", "validate", variant=variant)
            counts[variant] = (manifest["policy_objects"], sorted(kinds))
        self.assertEqual(counts["matchconditions"], counts["selectors"])

    def test_wait_targets_follow_the_mode(self):
        tmp = tempfile.mkdtemp()
        render.render("kyverno-cel", 4, POLICIES, Path(tmp), "matchconditions", "validate")
        targets = (Path(tmp) / "wait-targets.tsv").read_text()
        self.assertIn("validatingpolicy", targets)
        self.assertNotIn("mutatingpolicy", targets)

    def test_isolation_expectations_follow_the_mode(self):
        tmp = tempfile.mkdtemp()
        render.render("kyverno-cel", 4, POLICIES, Path(tmp), "matchconditions", "validate")
        mutate, validate = isolation.expected_policy_names(str(Path(tmp) / "rendered.json"))
        self.assertEqual(mutate, set())
        self.assertEqual(len(validate), 4)

    def test_webhook_expectation_follows_engine_and_mode(self):
        count = isolation.expected_pod_webhook_count
        # Kyverno's webhooks are generated from the installed policies.
        for engine in ("kyverno", "kyverno-cel"):
            self.assertEqual(count(engine, "both"), 2)
            self.assertEqual(count(engine, "validate"), 1)
            self.assertEqual(count(engine, "mutate"), 1)
        # Gatekeeper's come from its chart and match every resource, so both
        # exist no matter which policies are installed.
        for mode in ("both", "validate", "mutate"):
            self.assertEqual(count("gatekeeper", mode), 2)

    def test_gatekeeper_template_expectation_follows_the_mode(self):
        # The ConstraintTemplate is validate-side: a mutate-only run renders no
        # template, so the isolation gate must not demand one.
        import tempfile
        from pathlib import Path as P

        for mode, wants_template in (("both", True), ("validate", True), ("mutate", False)):
            tmp = tempfile.mkdtemp()
            render.render("gatekeeper", 4, POLICIES, P(tmp), "matchconditions", mode)
            _, validate = isolation.expected_policy_names(str(P(tmp) / "rendered.json"))
            with self.subTest(mode=mode):
                self.assertEqual(bool(validate), wants_template)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(SystemExit):
            render.render("kyverno-cel", 1, POLICIES, Path(tempfile.mkdtemp()), "matchconditions", "nope")

    def test_count_one_still_renders_each_half(self):
        for mode, expected in (("both", 2), ("validate", 1), ("mutate", 1)):
            manifest, _, _ = render_arm("kyverno-cel", mode, count=1)
            with self.subTest(mode=mode):
                self.assertEqual(manifest["policy_objects"], expected)


if __name__ == "__main__":
    unittest.main()
