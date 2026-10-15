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


aggregate = load_module("benchmark_aggregate", ROOT / "scripts" / "aggregate.py")
normalize = aggregate.normalize_digest

DIGEST = "sha256:5a0704f0b2b5a5d3181e8d466ac4f4d0ea8762a3d499db72c2f6516afad26e7a"
OTHER = "sha256:608780c8bb58396aaefcec559b0064edd6f508adb898d0f7953274cdd13d3e37"


class ImportRepositoryNormalization(unittest.TestCase):
    def test_same_image_imported_either_side_of_midnight_matches(self):
        before = f"docker.io/library/import-2026-10-13@{DIGEST}"
        after = f"docker.io/library/import-2026-10-14@{DIGEST}"
        self.assertEqual(normalize(before), normalize(after))

    def test_different_content_still_differs(self):
        a = f"docker.io/library/import-2026-10-13@{DIGEST}"
        b = f"docker.io/library/import-2026-10-13@{OTHER}"
        self.assertNotEqual(normalize(a), normalize(b))

    def test_a_real_registry_digest_is_left_alone(self):
        pulled = f"ghcr.io/kyverno/kyverno@{DIGEST}"
        self.assertEqual(normalize(pulled), pulled)

    def test_two_different_real_repositories_still_differ(self):
        a = f"ghcr.io/kyverno/kyverno@{DIGEST}"
        b = f"docker.io/kyverno/kyverno@{DIGEST}"
        self.assertNotEqual(normalize(a), normalize(b))

    def test_a_date_like_tag_outside_the_import_prefix_is_untouched(self):
        tagged = f"example.com/import-2026-10-13-tools@{DIGEST}"
        self.assertEqual(normalize(tagged), tagged)


if __name__ == "__main__":
    unittest.main()
