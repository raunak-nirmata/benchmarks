from __future__ import annotations

import importlib.util
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

COUNT = 8


def render_distinct(mode: str = "mutate", count: int = COUNT) -> str:
    tmp = tempfile.mkdtemp()
    render.render("kyverno-cel", count, POLICIES, Path(tmp), "distinct", mode)
    return (Path(tmp) / "policies.yaml").read_text()


def patch_keys(text: str) -> list[str]:
    """Annotation keys each copy writes, in order."""
    return re.findall(r'escapeKey\("([a-z0-9-]+)"\)', text)


class DistinctVariant(unittest.TestCase):
    def test_each_copy_writes_its_own_annotation(self):
        text = render_distinct()
        own = [k for k in patch_keys(text) if k.startswith("policy-bench-mutate")]
        self.assertEqual(len(own), COUNT)
        self.assertEqual(len(set(own)), COUNT, "copies share an annotation key")
        for index in range(1, COUNT + 1):
            self.assertIn(f"policy-bench-mutate-{index:03d}", own)

    def test_every_copy_still_writes_the_contract_annotation(self):
        text = render_distinct()
        contract = [k for k in patch_keys(text) if k == "policy-bench-injected"]
        self.assertEqual(len(contract), COUNT)

    def test_the_contract_token_is_never_renamed(self):
        text = render_distinct()
        self.assertNotIn("policy-bench-injected-0", text)

    def test_it_stays_batched(self):
        # A matchCondition would give each copy its own webhook and defeat the
        # point of the variant.
        text = render_distinct()
        self.assertNotIn("matchConditions", text)
        self.assertIn("namespaceSelector", text)

    def test_count_one_writes_the_unsuffixed_key(self):
        text = render_distinct(count=1)
        own = [k for k in patch_keys(text) if k.startswith("policy-bench-mutate")]
        self.assertEqual(own, ["policy-bench-mutate"])

    def test_validate_half_matches_the_selectors_variant(self):
        # Only the mutating half differs; the validating half must stay identical
        # so a validate-only run is unaffected by the choice of variant.
        tmp_a, tmp_b = tempfile.mkdtemp(), tempfile.mkdtemp()
        render.render("kyverno-cel", 4, POLICIES, Path(tmp_a), "distinct", "validate")
        render.render("kyverno-cel", 4, POLICIES, Path(tmp_b), "selectors", "validate")
        a = (Path(tmp_a) / "policies.yaml").read_text()
        b = (Path(tmp_b) / "policies.yaml").read_text()
        strip = lambda s: "\n".join(
            l for l in s.splitlines() if not l.lstrip().startswith("#")
        ).strip()
        self.assertEqual(strip(a), strip(b))


if __name__ == "__main__":
    unittest.main()
