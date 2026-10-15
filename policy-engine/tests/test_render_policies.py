from __future__ import annotations

import importlib.util
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

SCALED_ARMS = ("kyverno", "kyverno-cel", "gatekeeper", "native")


class RenderPoliciesTests(unittest.TestCase):
    def render(self, engine: str, count: int) -> tuple[dict, Path]:
        directory = Path(tempfile.mkdtemp())
        manifest = render.render(engine, count, POLICIES, directory)
        return manifest, directory

    def test_count_one_is_byte_identical_to_the_templates(self):
        """The published profile must stay reproducible through the renderer."""
        for engine in SCALED_ARMS:
            manifest, directory = self.render(engine, 1)
            for item in manifest["files"]:
                template = (POLICIES / item["template"]).read_bytes()
                rendered = (directory / item["path"]).read_bytes()
                self.assertEqual(template, rendered, f"{engine}/{item['path']} drifted at count 1")

    def test_each_copy_is_named_once_per_index(self):
        for engine in SCALED_ARMS:
            manifest, _ = self.render(engine, 4)
            self.assertEqual(len(manifest["names"]), len(set(manifest["names"])))
            for name in manifest["names"]:
                self.assertRegex(name, r"^policy-bench-(mutate|validate)-00[1-4]$")

    def test_policy_object_count_scales_linearly(self):
        for engine in SCALED_ARMS:
            one, _ = self.render(engine, 1)
            eight, _ = self.render(engine, 8)
            self.assertEqual(eight["policy_objects"], one["policy_objects"] * 8)

    def test_contract_tokens_survive_renaming(self):
        """Renaming policies must not touch the namespace, label, or annotation keys."""
        for engine in SCALED_ARMS:
            manifest, directory = self.render(engine, 3)
            for item in manifest["files"]:
                template_text = (POLICIES / item["template"]).read_text()
                rendered_text = (directory / item["path"]).read_text()
                for token in render.CONTRACT_TOKENS + ("policy-bench",):
                    if token in template_text:
                        self.assertIn(token, rendered_text, f"{engine} lost {token}")
                if "policy-bench-team annotation must equal platform" in template_text:
                    self.assertIn(
                        "policy-bench-team annotation must equal platform", rendered_text
                    )

    def test_binding_references_follow_the_rename(self):
        """A native binding that still pointed at the base name would bind nothing."""
        _, directory = self.render("native", 3)
        text = (directory / "policies.yaml").read_text()
        for index in range(1, 4):
            self.assertIn(f"policyName: policy-bench-mutate-{index:03d}", text)
            self.assertIn(f"policyName: policy-bench-validate-{index:03d}", text)
        self.assertNotIn("policyName: policy-bench-mutate\n", text)

    def test_gatekeeper_constraint_template_stays_single(self):
        """The template defines the constraint kind; duplicating it would conflict."""
        manifest, directory = self.render("gatekeeper", 5)
        template_entry = next(
            item for item in manifest["files"] if item["path"] == "template.yaml"
        )
        self.assertFalse(template_entry["scaled"])
        self.assertEqual(
            (POLICIES / "gatekeeper" / "template.yaml").read_bytes(),
            (directory / "template.yaml").read_bytes(),
        )
        constraints = (directory / "constraint.yaml").read_text()
        self.assertEqual(constraints.count("kind: PolicyBenchRequireTeam"), 5)

    def test_wait_targets_cover_every_kyverno_policy(self):
        for engine in ("kyverno", "kyverno-cel"):
            manifest, directory = self.render(engine, 3)
            self.assertEqual(len(manifest["wait_targets"]), 6)
            lines = (directory / "wait-targets.tsv").read_text().splitlines()
            self.assertEqual(len(lines), 6)
            for line in lines:
                kind, name = line.split("\t")
                self.assertIn(name, manifest["names"])
                self.assertTrue(kind)

    def test_arms_without_ready_conditions_have_no_wait_targets(self):
        for engine in ("gatekeeper", "native", "baseline"):
            manifest, directory = self.render(engine, 2)
            self.assertEqual(manifest["wait_targets"], [])
            self.assertEqual((directory / "wait-targets.tsv").read_text(), "")

    def test_baseline_renders_nothing(self):
        manifest, _ = self.render("baseline", 16)
        self.assertEqual(manifest["files"], [])
        self.assertEqual(manifest["names"], [])

    def test_selectors_variant_declares_no_match_conditions(self):
        """A CEL policy with matchConditions gets its own webhook endpoint.

        Kyverno's webhook controller classifies such a policy as fine-grained
        (buildWebhookRules in pkg/controllers/webhook/validating.go), so the
        apiserver calls the webhook once per policy and the engine never sees a
        multi-policy request. The selectors variant must therefore declare none.
        """
        directory = Path(tempfile.mkdtemp())
        render.render("kyverno-cel", 4, POLICIES, directory, "selectors")
        text = (directory / "policies.yaml").read_text()
        keys = [line for line in text.splitlines() if line.strip().startswith("matchConditions:")]
        self.assertEqual(keys, [], "selectors variant must not declare matchConditions")
        self.assertEqual(
            len([l for l in text.splitlines() if l.strip().startswith("matchConstraints:")]), 8
        )
        self.assertIn("namespaceSelector", text)
        self.assertIn("objectSelector", text)

    def test_default_variant_still_uses_match_conditions(self):
        directory = Path(tempfile.mkdtemp())
        render.render("kyverno-cel", 1, POLICIES, directory)
        text = (directory / "policies.yaml").read_text()
        self.assertTrue(any(l.strip().startswith("matchConditions:") for l in text.splitlines()))

    def test_both_variants_declare_the_same_policy_names(self):
        """The isolation gate expects these names, so the variants must agree."""
        default_dir = Path(tempfile.mkdtemp())
        selector_dir = Path(tempfile.mkdtemp())
        a = render.render("kyverno-cel", 5, POLICIES, default_dir)
        b = render.render("kyverno-cel", 5, POLICIES, selector_dir, "selectors")
        self.assertEqual(a["names"], b["names"])
        self.assertEqual(a["wait_targets"], b["wait_targets"])

    def test_selectors_variant_keeps_the_contract_tokens(self):
        directory = Path(tempfile.mkdtemp())
        render.render("kyverno-cel", 2, POLICIES, directory, "selectors")
        text = (directory / "policies.yaml").read_text()
        for token in render.CONTRACT_TOKENS + ("policy-bench",):
            self.assertIn(token, text)
        self.assertIn("policy-bench-team annotation must equal platform", text)

    def test_unknown_variant_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                render.render("kyverno-cel", 1, POLICIES, Path(directory), "nonsense")

    def test_invalid_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                render.render("unknown-engine", 1, POLICIES, Path(directory))
            with self.assertRaises(SystemExit):
                render.render("kyverno", 0, POLICIES, Path(directory))

class IsolationInventoryScalesTests(unittest.TestCase):
    """The isolation gate must expect exactly the objects the renderer produced.

    A name-exact expectation would fail every cell above one policy pair, which
    withholds the whole report.
    """

    def expected(self, engine: str, count: int) -> tuple[set[str], set[str]]:
        directory = Path(tempfile.mkdtemp())
        render.render(engine, count, POLICIES, directory)
        return isolation.expected_policy_names(str(directory / "rendered.json"))

    def test_expectation_matches_rendered_names(self):
        for engine in SCALED_ARMS:
            mutate, validate = self.expected(engine, 6)
            # Gatekeeper splits the pair across a Constraint and a mutator, but
            # the rendered count per side is the same for every arm.
            self.assertEqual(len(mutate), 6)
            self.assertEqual(len(validate), 6)
            self.assertTrue(all(name.startswith("policy-bench-mutate") for name in mutate))
            self.assertTrue(all(name.startswith("policy-bench-validate") for name in validate))

    def test_single_pair_expectation_is_the_published_pair(self):
        mutate, validate = self.expected("kyverno", 1)
        self.assertEqual(mutate, {"policy-bench-mutate"})
        self.assertEqual(validate, {"policy-bench-validate"})

    def test_missing_manifest_falls_back_to_the_published_pair(self):
        mutate, validate = isolation.expected_policy_names(None)
        self.assertEqual(mutate, {"policy-bench-mutate"})
        self.assertEqual(validate, {"policy-bench-validate"})


if __name__ == "__main__":
    unittest.main()
