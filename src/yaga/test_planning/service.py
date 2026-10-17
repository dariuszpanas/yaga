"""Installed orchestration of explicit policy, Git source, and bounded planning."""

from __future__ import annotations

from pathlib import Path

from yaga.errors import InputError
from yaga.test_planning.git import collect_test_source
from yaga.test_planning.models import SelectionMode
from yaga.test_planning.planner import plan_tests, resolve_profile
from yaga.test_planning.policy import load_test_policy
from yaga.test_planning.reporting import test_plan_document
from yaga.test_planning.reports import PlannedTests, validate_bindings_sha256


def create_test_plan(
    repository: Path,
    *,
    policy_path: Path,
    profile: str,
    revision_range: str | None = None,
    revision: str | None = None,
    bindings_sha256: str | None = None,
) -> PlannedTests:
    """Bind a complete pure selection to exact input identities without executing it."""
    if (revision_range is None) == (revision is None):
        raise InputError("test planning requires exactly one of --range or --revision")
    bindings_sha256 = validate_bindings_sha256(bindings_sha256)
    loaded = load_test_policy(policy_path)
    selected_profile = resolve_profile(loaded.policy, profile)
    if revision is not None and selected_profile.mode is not SelectionMode.FULL:
        raise InputError(
            "--revision requires a full test profile; use --range for selective planning"
        )
    collected = collect_test_source(repository, revision_range=revision_range, revision=revision)
    selection = plan_tests(loaded.policy, profile=profile, changed_paths=collected.changed_paths)
    planned = PlannedTests(
        policy_sha256=loaded.sha256,
        bindings_sha256=bindings_sha256,
        source=collected.source,
        selection=selection,
    )
    # Apply the complete envelope bound even for quiet callers or text output.
    test_plan_document(planned)
    return planned
