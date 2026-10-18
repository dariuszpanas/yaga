"""Demonstrate fixed job handoff for the two documented catalogs; execute nothing.

The definition digest covers this example's exact source bytes. It does not pin a
consumer's real tools or workflows. A production adapter must instead hash its
complete immutable execution definitions and preserve its existing result gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from yaga.errors import YagaError
from yaga.test_planning.handoff import ExecutorRegistry, SuiteExecutor, create_handoff
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.service import create_test_plan

# These are fixed reviewed job identifiers, never commands from a policy file.
# Each tuple is (suite, binding, dispatch, variants, available capabilities).
POLYGLOT_EXECUTORS = (
    ("repository.smoke", "repository-smoke", "repository", ("default",), ("linux",)),
    ("docs.check", "docs-check", "docs", ("default",), ()),
    ("backend.unit", "backend-unit", "backend-unit", ("py312", "py315"), ("linux", "python")),
    (
        "backend.integration",
        "backend-integration",
        "backend-integration",
        ("py312-postgres",),
        ("linux", "python", "postgres"),
    ),
    ("frontend.unit", "frontend-unit", "frontend-unit", ("node-supported",), ("node",)),
    (
        "frontend.browser",
        "frontend-browser",
        "frontend-browser",
        ("chromium",),
        ("node", "browser"),
    ),
    ("image.build", "image-build", "image", ("linux-amd64",), ("linux", "docker")),
    ("container.smoke", "container-smoke", "container", ("linux-amd64",), ("linux", "docker")),
    (
        "infra.chainsaw",
        "infra-chainsaw",
        "chainsaw",
        ("kind-supported",),
        ("linux", "docker", "kubernetes"),
    ),
    (
        "qualification.ray",
        "ray-qualification",
        "ray",
        ("linux-ray-supported",),
        ("linux", "docker", "ray"),
    ),
)
DJANGO_RAY_EXECUTORS = (
    (
        "qualification.application",
        "application-qualification",
        "application",
        ("default",),
        ("linux", "docker", "kind", "helm", "chainsaw"),
    ),
    (
        "qualification.native",
        "upgrade-native-qualification",
        "native",
        ("native", "jobs", "jobs-crash", "core-loss"),
        ("linux", "docker"),
    ),
    (
        "qualification.latency",
        "latency-qualification",
        "latency",
        ("default",),
        ("linux", "docker"),
    ),
    (
        "qualification.cancellation",
        "cancellation-qualification",
        "cancellation",
        ("default",),
        ("linux", "docker"),
    ),
    (
        "qualification.monitoring",
        "monitoring-qualification",
        "monitoring",
        ("default",),
        ("linux", "docker", "kind", "helm"),
    ),
)


def example_registry(catalog: str) -> ExecutorRegistry:
    """Build the reviewed example map without deriving permissions from policy."""
    if catalog == "polyglot":
        definitions = POLYGLOT_EXECUTORS
    elif catalog == "django-ray":
        definitions = DJANGO_RAY_EXECUTORS
    else:
        raise ValueError("unknown example catalog")
    definition_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return ExecutorRegistry(
        tuple(SuiteExecutor(*definition, definition_sha256) for definition in definitions)
    )


def main() -> int:
    """Create one bounded handoff from explicit source and expected identities."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, choices=("polyglot", "django-ray"))
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--range", dest="revision_range")
    source.add_argument("--revision")
    parser.add_argument("--execution-commit", required=True)
    parser.add_argument("--execution-tree", required=True)
    args = parser.parse_args()
    try:
        registry = example_registry(args.catalog)
        expected_policy = load_test_policy(args.policy)
        planned = create_test_plan(
            args.repo,
            policy_path=args.policy,
            profile=args.profile,
            revision_range=args.revision_range,
            revision=args.revision,
            bindings_sha256=registry.sha256,
        )
        handoff = create_handoff(
            planned,
            registry,
            expected_policy_sha256=expected_policy.sha256,
            expected_execution_commit=args.execution_commit,
            expected_execution_tree=args.execution_tree,
        )
    except YagaError as error:
        # This example avoids reflecting untrusted paths/messages into terminals.
        print(f"test handoff failed ({error.kind})", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(handoff, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
