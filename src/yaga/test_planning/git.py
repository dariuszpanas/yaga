"""Explicit bounded Git source collection for installed test planning."""

from __future__ import annotations

from pathlib import Path

from yaga.errors import InputError
from yaga.git.changes import read_changed_paths, split_revision_range
from yaga.git.tree import resolve_committed_tree
from yaga.test_planning.source import CollectedTestSource, PlanSource


def collect_test_source(
    repository: Path,
    *,
    revision_range: str | None = None,
    revision: str | None = None,
) -> CollectedTestSource:
    """Bind one explicit range or commit-ish to its exact execution commit/tree.

    Range history must be complete. A revision-only source requires only its
    selected objects, including in shallow repositories. No source is inferred,
    fetched, or selected from the working tree or index.
    """
    if (revision_range is None) == (revision is None):
        raise InputError("test planning requires exactly one explicit range or revision")
    if revision_range is not None:
        base, _, head = split_revision_range(revision_range)
        if ":" in base or ":" in head:
            raise InputError("test planning range endpoints must not contain object-path selectors")
        comparison = read_changed_paths(repository, revision_range, strict_output=True)
        # Resolve the already selected OID, never the possibly moved symbolic ref.
        identity = resolve_committed_tree(repository, comparison.head_sha)
        return CollectedTestSource(
            source=PlanSource(
                base_commit=comparison.base_sha,
                head_commit=comparison.head_sha,
                comparison_commit=comparison.comparison_sha,
                execution_commit=identity.commit_sha,
                execution_tree=identity.tree_sha,
            ),
            changed_paths=comparison.paths,
        )
    assert revision is not None
    identity = resolve_committed_tree(repository, revision)
    return CollectedTestSource(
        source=PlanSource(
            base_commit=None,
            head_commit=None,
            comparison_commit=None,
            execution_commit=identity.commit_sha,
            execution_tree=identity.tree_sha,
        ),
        changed_paths=None,
    )
