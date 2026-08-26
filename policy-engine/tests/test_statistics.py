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
auditor = load_module("benchmark_auditor", ROOT / "scripts" / "audit-raw-results.py")


class PercentileTests(unittest.TestCase):
    def test_linear_interpolation(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(loadgen.percentile(values, 0.50), 2.5)
        self.assertAlmostEqual(loadgen.percentile(values, 0.95), 3.85)
        self.assertAlmostEqual(auditor.percentile(values, 0.95), 3.85)

    def test_order_does_not_matter(self):
        self.assertEqual(loadgen.percentile([9.0, 1.0, 5.0], 0.50), 5.0)

    def test_single_value(self):
        self.assertEqual(loadgen.percentile([7.0], 0.99), 7.0)

    def test_empty_loadgen_distribution(self):
        result = loadgen.distribution([])
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["p95"])


if __name__ == "__main__":
    unittest.main()
