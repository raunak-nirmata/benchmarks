from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRESETS = ROOT / "config" / "presets"
PUBLISHED_ENGINES = {"baseline", "kyverno", "gatekeeper", "native"}

# Build overrides are absolute paths on one machine, so a committed preset that
# carried them would be unreproducible anywhere else.
FORBIDDEN_KEYS = (
    "KYVERNO_CHART_PATH",
    "KYVERNO_IMAGE_REGISTRY",
    "KYVERNO_IMAGE_REPOSITORY",
    "KYVERNO_IMAGE_TAG",
    "KYVERNO_INIT_IMAGE_REPOSITORY",
    "KYVERNO_INIT_IMAGE_TAG",
)


def preset_names() -> list[str]:
    return sorted(path.stem for path in PRESETS.glob("*.env"))


def resolve(preset: str | None, env: dict[str, str] | None = None) -> dict[str, str]:
    """Source common.sh the way a script does, and report what the knobs became."""
    keys = (
        "ENGINES", "POLICY_COUNT", "KYVERNO_CEL_VARIANT", "PAYLOAD_PROFILE",
        "PAYLOAD_PADDING_BYTES", "TARGET_RPS", "TRIALS", "PROFILE",
    )
    script = "source scripts/common.sh\n" + "".join(
        f'printf "%s=%s\\n" {key} "${key}"\n' for key in keys
    )
    environ = dict(os.environ)
    environ.pop("PRESET", None)
    for key in keys:
        environ.pop(key, None)
    if preset is not None:
        environ["PRESET"] = preset
    environ.update(env or {})
    result = subprocess.run(
        ["bash", "-c", script], cwd=ROOT, env=environ,
        capture_output=True, text=True, check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


class PresetFiles(unittest.TestCase):
    def test_presets_exist(self):
        self.assertTrue(preset_names(), "no presets found")

    def test_every_assignment_defers_to_the_environment(self):
        # A plain VAR=value would clobber an explicit override, which would make
        # `PRESET=x TRIALS=1` silently run three trials.
        for name in preset_names():
            for line in (PRESETS / f"{name}.env").read_text().splitlines():
                if not line or line.startswith("#"):
                    continue
                with self.subTest(preset=name, line=line):
                    self.assertRegex(line, r"^([A-Z_]+)=\$\{\1:-.*\}$")

    def test_presets_carry_no_machine_specific_build_overrides(self):
        for name in preset_names():
            text = (PRESETS / f"{name}.env").read_text()
            for key in FORBIDDEN_KEYS:
                with self.subTest(preset=name, key=key):
                    self.assertNotIn(f"{key}=", text)

    def test_every_preset_is_documented(self):
        readme = (PRESETS / "README.md").read_text()
        for name in preset_names():
            with self.subTest(preset=name):
                self.assertIn(f"`{name}`", readme)


class PresetResolution(unittest.TestCase):
    def test_no_preset_reproduces_the_published_profile(self):
        resolved = resolve(None)
        self.assertEqual(set(resolved["ENGINES"].split()), PUBLISHED_ENGINES)
        self.assertEqual(resolved["PROFILE"], "exact")
        self.assertEqual(resolved["POLICY_COUNT"], "1")
        self.assertEqual(resolved["KYVERNO_CEL_VARIANT"], "matchconditions")
        self.assertEqual(resolved["PAYLOAD_PROFILE"], "minimal")
        self.assertEqual(resolved["PAYLOAD_PADDING_BYTES"], "0")

    def test_preset_overrides_the_default(self):
        resolved = resolve("five-arm-batched-n16")
        self.assertEqual(resolved["POLICY_COUNT"], "16")
        self.assertEqual(resolved["KYVERNO_CEL_VARIANT"], "selectors")
        self.assertIn("kyverno-cel", resolved["ENGINES"].split())

    def test_environment_overrides_the_preset(self):
        resolved = resolve("five-arm-batched-n16", {"TRIALS": "1", "TARGET_RPS": "50"})
        self.assertEqual(resolved["TRIALS"], "1")
        self.assertEqual(resolved["TARGET_RPS"], "50")
        self.assertEqual(resolved["POLICY_COUNT"], "16")

    def test_unknown_preset_fails_and_lists_the_real_ones(self):
        result = subprocess.run(
            ["bash", "-c", "source scripts/common.sh"],
            cwd=ROOT, env={**os.environ, "PRESET": "no-such-preset"},
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown preset", result.stderr)
        for name in preset_names():
            self.assertIn(name, result.stderr)

    def test_no_preset_claims_to_be_an_article_reproduction(self):
        # Every preset changes the shape in some way, so none may resolve to a
        # configuration that aggregate.py would label a reproduction.
        for name in preset_names():
            resolved = resolve(name)
            with self.subTest(preset=name):
                reproduction = (
                    resolved["PROFILE"] == "exact"
                    and set(resolved["ENGINES"].split()) == PUBLISHED_ENGINES
                    and resolved["POLICY_COUNT"] == "1"
                    and resolved["PAYLOAD_PADDING_BYTES"] == "0"
                    and resolved["KYVERNO_CEL_VARIANT"] == "matchconditions"
                    and resolved["PAYLOAD_PROFILE"] == "minimal"
                )
                self.assertFalse(reproduction)


class ExactProfileGate(unittest.TestCase):
    """PROFILE=exact must reject an extra arm, not just a missing one."""

    def run_gate(self, engines: str, profile: str) -> subprocess.CompletedProcess:
        """Exercise the engine-set gate without ever starting a benchmark.

        The gate runs before the results-directory check, so pointing the script
        at a directory that is already non-empty gives a hard stop immediately
        after validation. Without that, a configuration the gate accepts would
        run a real benchmark on any host where the tool cache exists.
        """
        with tempfile.TemporaryDirectory() as tmp:
            stub_bin = Path(tmp) / "bin"
            stub_bin.mkdir()
            for tool in ("kind", "kubectl", "helm", "docker"):
                stub = stub_bin / tool
                stub.write_text("#!/usr/bin/env bash\nexit 0\n")
                stub.chmod(0o755)
            occupied = Path(tmp) / "run"
            occupied.mkdir()
            (occupied / "already-here").write_text("stop here\n")
            env = dict(os.environ)
            env.pop("PRESET", None)
            env["PATH"] = f"{stub_bin}:{env['PATH']}"
            env["ENGINES"] = engines
            env["PROFILE"] = profile
            result = subprocess.run(
                ["bash", "scripts/run-benchmark.sh", str(occupied)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
            )
            # Whatever the gate decided, the script must not have proceeded.
            assert result.returncode != 0, result.stdout
            return result

    def test_exact_rejects_an_extra_arm(self):
        result = self.run_gate("baseline kyverno kyverno-cel gatekeeper native", "exact")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not allow the kyverno-cel arm", result.stderr)

    def test_exact_rejects_a_missing_arm(self):
        result = self.run_gate("baseline kyverno gatekeeper", "exact")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly once", result.stderr)

    def test_scaling_allows_the_extra_arm(self):
        result = self.run_gate("baseline kyverno-cel", "scaling")
        self.assertNotIn("does not allow", result.stderr)
        # It got past the gate and stopped on the directory check instead.
        self.assertIn("result directory is not empty", result.stderr)


if __name__ == "__main__":
    unittest.main()
