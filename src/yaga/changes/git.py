"""Changed-path coupling adapter for the shared bounded Git collector."""

from __future__ import annotations

from pathlib import Path

from yaga.changes.models import ChangeSelection
from yaga.git.changes import read_changed_paths as collect_changed_paths


def read_changed_paths(repository: Path, revision_range: str) -> ChangeSelection:
    """Resolve an exact range and retain the coupling provider's selection contract."""
    selected = collect_changed_paths(repository, revision_range)
    return ChangeSelection(
        revision_range=selected.revision_range,
        base_sha=selected.base_sha,
        head_sha=selected.head_sha,
        comparison_sha=selected.comparison_sha,
        paths=selected.paths,
    )
