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
expect = loadgen.case_expectations

CASES = (
    "matched-valid",
    "matched-missing-team",
    "matched-wrong-team",
    "unmatched-label",
    "unmatched-namespace",
    "no-overwrite",
)


class CaseExpectations(unittest.TestCase):
    def test_every_case_is_covered_in_every_mode(self):
        for mode in ("both", "validate", "mutate"):
            for engine in ("kyverno-cel", "baseline"):
                with self.subTest(mode=mode, engine=engine):
                    self.assertEqual(set(expect(engine, mode)), set(CASES))

    def test_both_halves_deny_invalid_and_annotate_valid(self):
        e = expect("kyverno-cel", "both")
        self.assertEqual(e["matched-valid"], (True, "true"))
        self.assertEqual(e["matched-missing-team"], (False, None))
        self.assertEqual(e["matched-wrong-team"], (False, None))

    def test_validate_only_denies_invalid_and_annotates_nothing(self):
        e = expect("kyverno-cel", "validate")
        self.assertEqual(e["matched-valid"], (True, None))
        self.assertEqual(e["matched-missing-team"], (False, None))

    def test_mutate_only_admits_invalid_and_still_annotates_it(self):
        # The regression this table was extracted for: with no validating policy
        # an invalid Pod is admitted, and the mutating policy applies to it, so
        # the annotation must be expected rather than forbidden.
        e = expect("kyverno-cel", "mutate")
        self.assertEqual(e["matched-valid"], (True, "true"))
        self.assertEqual(e["matched-missing-team"], (True, "true"))
        self.assertEqual(e["matched-wrong-team"], (True, "true"))

    def test_baseline_never_denies_or_annotates(self):
        for mode in ("both", "validate", "mutate"):
            e = expect("baseline", mode)
            with self.subTest(mode=mode):
                for case in CASES:
                    admitted, injected = e[case]
                    self.assertTrue(admitted)
                    self.assertEqual(injected, "false" if case == "no-overwrite" else None)

    def test_unmatched_pods_are_untouched_in_every_mode(self):
        for mode in ("both", "validate", "mutate"):
            e = expect("kyverno-cel", mode)
            with self.subTest(mode=mode):
                self.assertEqual(e["unmatched-label"], (True, None))
                self.assertEqual(e["unmatched-namespace"], (True, None))

    def test_no_overwrite_is_preserved_in_every_mode(self):
        for mode in ("both", "validate", "mutate"):
            with self.subTest(mode=mode):
                self.assertEqual(expect("kyverno-cel", mode)["no-overwrite"], (True, "false"))

    def test_an_admitted_case_never_expects_a_missing_annotation_when_mutating(self):
        # Guards the shape of the bug generally: if a case is admitted and the
        # mutating half is installed, a matched Pod must carry the annotation.
        e = expect("kyverno-cel", "mutate")
        for case in ("matched-valid", "matched-missing-team", "matched-wrong-team"):
            admitted, injected = e[case]
            self.assertTrue(admitted)
            self.assertIsNotNone(injected, case)


if __name__ == "__main__":
    unittest.main()
