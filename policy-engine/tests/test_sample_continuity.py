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


aggregate = load_module("benchmark_aggregate", ROOT / "scripts" / "aggregate.py")


def series(gaps_after: dict[int, float], count: int = 61) -> Path:
    """A resource file with one sample per second, plus injected gaps."""
    import datetime as dt

    start = dt.datetime(2026, 10, 14, 6, 0, 0, tzinfo=dt.timezone.utc)
    rows, offset = [], 0.0
    for index in range(count):
        stamp = start + dt.timedelta(seconds=offset)
        rows.append(
            json.dumps(
                {
                    "timestamp": stamp.isoformat(),
                    "node": {"cpu_cores": 0.4, "memory_working_set_bytes": 1},
                    "containers": [],
                }
            )
        )
        offset += gaps_after.get(index, 1.0)
    path = Path(tempfile.mkdtemp()) / "resources-load.jsonl"
    path.write_text("\n".join(rows) + "\n")
    return path


class SampleContinuity(unittest.TestCase):
    def test_a_clean_series_is_accepted(self):
        aggregate.read_resources(series({}), "baseline", 1.0)

    def test_a_suspension_is_rejected(self):
        # The real case: the host slept for 68.7 s mid-measurement and every
        # other gate passed.
        path = series({30: 68.7})
        with self.assertRaises(SystemExit) as caught:
            aggregate.read_resources(path, "baseline", 1.0)
        self.assertIn("sampling gap", str(caught.exception))
        self.assertIn("68.7", str(caught.exception))

    def test_small_jitter_is_tolerated(self):
        # Sampling is not perfectly periodic; only a clock jump should fail.
        aggregate.read_resources(series({10: 1.4, 20: 2.2}), "baseline", 1.0)

    def test_the_tolerance_scales_with_the_configured_interval(self):
        # A 4 s gap is a suspension at 1 Hz and normal at a 5 s interval.
        path = series({10: 4.0})
        with self.assertRaises(SystemExit):
            aggregate.read_resources(path, "baseline", 1.0)
        aggregate.read_resources(series({10: 4.0}), "baseline", 5.0)


if __name__ == "__main__":
    unittest.main()
