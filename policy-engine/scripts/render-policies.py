#!/usr/bin/env python3
"""Render N identical copies of an engine's contract policies.

The published profile installs one mutate and one validate policy per arm. The
policy-count axis installs N copies instead, so an engine's per-request cost can
be measured as a function of how many policies match the same object.

At count 1 the templates are copied byte for byte, so the published profile is
reproduced exactly. Above 1, every policy name gains a zero-padded suffix and
nothing else changes: the match blocks, the mutation value, and the denial
message stay identical, so the CONTRACT.md truth table still holds. Renaming is
a whole-token substitution of the two policy names, which also rewrites the
binding references (`policyName:`) that point at them.

Output goes to a per-cell directory, never back into `policies/`, because
`hash-inputs.sh` hashes that tree and the run aborts if it changes mid-run.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

# The two policy names every arm shares. Both are whole tokens; neither is a
# prefix of the other, and the namespace (`policy-bench`) and the annotation and
# label keys (`policy-bench-team`, `policy-bench-subject`,
# `policy-bench-injected`) are distinct tokens that must never be rewritten.
POLICY_NAMES = ("policy-bench-mutate", "policy-bench-validate")

# Template files per arm, and whether each scales with the policy count. A
# Gatekeeper ConstraintTemplate defines the constraint kind, so it stays single;
# the Constraints and AssignMetadata objects that use it are what multiply.
# The third element is the aspect: which half of the contract the file
# implements. "mixed" means the file holds both halves and must be split by
# document. A Gatekeeper ConstraintTemplate carries no policy name but exists
# only to define the kind the Constraints use, so it is validate-side.
TEMPLATES: dict[str, tuple[tuple[str, bool, str], ...]] = {
    "baseline": (),
    "kyverno": (("kyverno/policies.yaml", True, "mixed"),),
    "kyverno-cel": (("kyverno-cel/policies.yaml", True, "mixed"),),
    "gatekeeper": (
        ("gatekeeper/template.yaml", False, "validate"),
        ("gatekeeper/constraint.yaml", True, "validate"),
        ("gatekeeper/mutation.yaml", True, "mutate"),
    ),
    "native": (("native/policies.yaml", True, "mixed"),),
}

# Which halves of the contract an arm installs. "both" is the published shape.
# "validate" exists because the two kyverno-cel variants are not equivalent on
# the mutating side: the matchConditions variant skips a patch whose annotation
# is already present, and mutating webhooks run in sequence over the patched
# object, so only the first copy executes. The validating side has no such
# condition and executes every copy in both variants, which makes validate-only
# the controlled comparison.
POLICY_MODES = ("both", "validate", "mutate")


def aspects_for(mode: str) -> frozenset[str]:
    if mode == "both":
        return frozenset({"mutate", "validate"})
    return frozenset({mode})


def document_aspect(text: str) -> str | None:
    """Which half of the contract one YAML document implements."""
    found = names_in(text)
    if "policy-bench-mutate" in found:
        return "mutate"
    if "policy-bench-validate" in found:
        return "validate"
    return None


def select_documents(text: str, aspect: str, mode: str) -> str:
    """Drop the documents this mode does not install. Empty means drop the file."""
    wanted = aspects_for(mode)
    if aspect != "mixed":
        return text if aspect in wanted else ""
    kept = [
        document
        for document in text.split("\n---\n")
        if document.strip() and document_aspect(document) in wanted
    ]
    return "\n---\n".join(kept)

# Variants of an arm's policy set. Kyverno gives a CEL policy its own webhook
# endpoint when it declares matchConditions, so the default set produces N webhook
# calls carrying one policy each. The selectors variant expresses the same contract
# through matchConstraints selectors, which keeps the policies in the shared webhook
# and makes one call carry all N - the only shape that exercises the engine's
# per-request policy loop.
VARIANTS: dict[tuple[str, str], tuple[tuple[str, bool, str], ...]] = {
    ("kyverno-cel", "selectors"): (("kyverno-cel-selectors/policies.yaml", True, "mixed"),),
    ("kyverno-cel", "distinct"): (("kyverno-cel-distinct/policies.yaml", True, "mixed"),),
}
DEFAULT_VARIANT = "matchconditions"


def templates_for(engine: str, variant: str) -> tuple[tuple[str, bool, str], ...]:
    if variant != DEFAULT_VARIANT and (engine, variant) in VARIANTS:
        return VARIANTS[(engine, variant)]
    return TEMPLATES[engine]


# Readiness targets, by arm. Only Kyverno's policy types report a ready
# condition; Gatekeeper constraints and native admission policies do not, and
# install-engine.sh gates those on the CRD and the settle window instead.
WAIT_KINDS: dict[str, dict[str, str]] = {
    "kyverno": {
        "policy-bench-mutate": "clusterpolicy",
        "policy-bench-validate": "clusterpolicy",
    },
    "kyverno-cel": {
        "policy-bench-mutate": "mutatingpolicy.policies.kyverno.io",
        "policy-bench-validate": "validatingpolicy.policies.kyverno.io",
    },
}


def copy_name(name: str, index: int) -> str:
    return f"{name}-{index:03d}"


def rename_document(text: str, index: int) -> str:
    for name in POLICY_NAMES:
        pattern = re.compile(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9-])")
        text = pattern.sub(copy_name(name, index), text)
    return text


def render_file(text: str, destination: Path, count: int, scalable: bool) -> None:
    if count == 1 or not scalable:
        destination.write_text(text)
        return
    copies = [rename_document(text, index) for index in range(1, count + 1)]
    destination.write_text("\n---\n".join(copies))


def names_in(text: str) -> list[str]:
    """Which policy names a template actually declares.

    Gatekeeper splits the pair across files: the Constraint carries only the
    validate name and the AssignMetadata only the mutate name.
    """
    return [
        name
        for name in POLICY_NAMES
        if re.search(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9-])", text)
    ]


# Contract tokens that renaming must never touch.
CONTRACT_TOKENS = ("policy-bench-team", "policy-bench-subject", "policy-bench-injected")


def check_rendering(destination: Path, template_text: str, count: int, scalable: bool) -> None:
    """Fail loudly rather than silently measuring the wrong policy count."""
    if not scalable or count == 1:
        return
    text = destination.read_text()
    for name in names_in(template_text):
        for index in range(1, count + 1):
            if copy_name(name, index) not in text:
                raise SystemExit(f"{destination}: missing rendered copy {copy_name(name, index)}")
        if re.search(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9-])", text):
            raise SystemExit(f"{destination}: unrenamed {name} would collide across copies")
    for token in CONTRACT_TOKENS:
        if token in template_text and token not in text:
            raise SystemExit(f"{destination}: renaming dropped contract token {token}")


def render(
    engine: str,
    count: int,
    policies_dir: Path,
    output_dir: Path,
    variant: str = DEFAULT_VARIANT,
    mode: str = "both",
) -> dict:
    if engine not in TEMPLATES:
        raise SystemExit(f"unknown engine: {engine}")
    if count < 1:
        raise SystemExit(f"POLICY_COUNT must be at least 1, got {count}")
    known_variants = {DEFAULT_VARIANT} | {v for _, v in VARIANTS}
    if variant not in known_variants:
        raise SystemExit(f"unknown policy variant: {variant}")
    if mode not in POLICY_MODES:
        raise SystemExit(f"unknown policy mode: {mode}")
    templates = templates_for(engine, variant)

    output_dir.mkdir(parents=True, exist_ok=True)
    files = []
    declared: list[tuple[str, str]] = []
    for relative, scalable, aspect in templates:
        template = policies_dir / relative
        if not template.is_file():
            raise SystemExit(f"missing policy template: {template}")
        template_text = select_documents(template.read_text(), aspect, mode)
        if not template_text.strip():
            # This mode does not install this half of the contract.
            continue
        destination = output_dir / Path(relative).name
        render_file(template_text, destination, count, scalable)
        check_rendering(destination, template_text, count, scalable)
        files.append(
            {
                "path": destination.name,
                "template": relative,
                "scaled": scalable,
                "aspect": aspect,
            }
        )
        if not scalable:
            continue
        for base in names_in(template_text):
            if count == 1:
                declared.append((base, base))
            else:
                declared.extend(
                    (base, copy_name(base, index)) for index in range(1, count + 1)
                )

    names = [name for _, name in declared]
    wait_targets = [
        (WAIT_KINDS[engine][base], name)
        for base, name in declared
        if base in WAIT_KINDS.get(engine, {})
    ]
    (output_dir / "wait-targets.tsv").write_text(
        "".join(f"{kind}\t{name}\n" for kind, name in wait_targets)
    )

    manifest = {
        "engine": engine,
        "variant": variant,
        "policy_mode": mode,
        "policy_count": count,
        "policy_objects": len(names),
        "files": files,
        "names": names,
        "wait_targets": [f"{kind}/{name}" for kind, name in wait_targets],
    }
    (output_dir / "rendered.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--variant", default=DEFAULT_VARIANT)
    parser.add_argument("--mode", default="both", choices=POLICY_MODES)
    parser.add_argument("--policies-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = render(
        args.engine, args.count, args.policies_dir, args.output_dir,
        args.variant, args.mode,
    )
    print(
        f"[policy-bench] rendered {manifest['policy_objects']} policy objects "
        f"for {args.engine} ({args.variant}, {args.mode}) at count {args.count}"
    )


if __name__ == "__main__":
    main()
