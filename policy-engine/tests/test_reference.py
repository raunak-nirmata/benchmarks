from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = json.loads(
            (ROOT / "reference" / "published-2026-08-19.json").read_text()
        )

    def test_request_total(self):
        shape = self.reference["load_shape"]
        total = (
            shape["requests_per_engine_per_trial"]
            * shape["trials"]
            * len(self.reference["engines"])
        )
        self.assertEqual(total, 144_000)

    def test_published_ordering_at_p95(self):
        engines = self.reference["engines"]
        ordered = sorted(
            engines,
            key=lambda name: engines[name]["http_latency_ms"]["p95"],
        )
        self.assertEqual(ordered, ["baseline", "native", "gatekeeper", "kyverno"])

    def test_each_engine_has_three_trial_values(self):
        for item in self.reference["engines"].values():
            self.assertEqual(len(item["trial_http_p95_ms"]), 3)


if __name__ == "__main__":
    unittest.main()
