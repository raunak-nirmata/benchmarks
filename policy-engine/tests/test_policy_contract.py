from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "kyverno": ROOT / "policies" / "kyverno" / "policies.yaml",
    "gatekeeper_template": ROOT / "policies" / "gatekeeper" / "template.yaml",
    "gatekeeper_constraint": ROOT / "policies" / "gatekeeper" / "constraint.yaml",
    "gatekeeper_mutation": ROOT / "policies" / "gatekeeper" / "mutation.yaml",
    "native": ROOT / "policies" / "native" / "policies.yaml",
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


isolation = load_module(
    "benchmark_verify_isolation", ROOT / "scripts" / "verify-isolation.py"
)


class PolicyContractStaticTests(unittest.TestCase):
    def test_gatekeeper_constraint_resource_matches_installed_crd(self):
        constraint_template = FILES["gatekeeper_template"].read_text()
        self.assertIn("kind: PolicyBenchRequireTeam", constraint_template)
        self.assertEqual(
            isolation.GATEKEEPER_CONSTRAINT_RESOURCE,
            "policybenchrequireteam.constraints.gatekeeper.sh",
        )

    def test_shared_contract_tokens(self):
        corpus = {name: path.read_text() for name, path in FILES.items()}
        for name, text in corpus.items():
            self.assertIn("policy-bench", text, name)

        combined = {
            "kyverno": corpus["kyverno"],
            "gatekeeper": "\n".join(
                corpus[name]
                for name in (
                    "gatekeeper_template",
                    "gatekeeper_constraint",
                    "gatekeeper_mutation",
                )
            ),
            "native": corpus["native"],
        }
        for name, text in combined.items():
            self.assertIn("policy-bench-subject", text, name)
            self.assertIn("policy-bench-injected", text, name)
            self.assertIn("policy-bench-team", text, name)
            self.assertIn("platform", text, name)
            self.assertIn("policy-bench-team annotation must equal platform", text, name)

    def test_explicit_operation_scope_where_language_supports_it(self):
        kyverno = FILES["kyverno"].read_text()
        native = FILES["native"].read_text()
        for text in (kyverno, native):
            self.assertIn("CREATE", text)
            self.assertIn("UPDATE", text)

    def test_no_overwrite_forms_are_present(self):
        self.assertIn("+(policy-bench-injected)", FILES["kyverno"].read_text())
        self.assertIn(
            "AssignMetadata", FILES["gatekeeper_mutation"].read_text()
        )
        self.assertIn(
            "annotation-not-already-present", FILES["native"].read_text()
        )


if __name__ == "__main__":
    unittest.main()
