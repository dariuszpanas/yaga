"""Immutable, validated source identities for versioned test plans."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.path_patterns import validate_path
from yaga.test_planning.limits import MAX_CHANGED_PATH_BYTES_TOTAL, MAX_CHANGED_PATHS

_LOWER_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class PlanSource:
    """Exact execution identity plus an optional complete Git comparison."""

    base_commit: str | None
    head_commit: str | None
    comparison_commit: str | None
    execution_commit: str
    execution_tree: str

    def __post_init__(self) -> None:
        comparison = (self.base_commit, self.head_commit, self.comparison_commit)
        if any(value is None for value in comparison) and any(
            value is not None for value in comparison
        ):
            raise ValueError("plan comparison identities must all be present or all be null")
        identities = [self.execution_commit, self.execution_tree]
        identities.extend(value for value in comparison if value is not None)
        if (
            any(not _is_object_id(value) for value in identities)
            or len({len(value) for value in identities}) != 1
        ):
            raise ValueError("plan source requires same-format lowercase object IDs")
        if self.head_commit is not None and self.head_commit != self.execution_commit:
            raise ValueError("plan execution commit must equal the range head")


@dataclass(frozen=True, slots=True)
class CollectedTestSource:
    """One exact source and its complete sorted change set, when requested."""

    source: PlanSource
    changed_paths: tuple[str, ...] | None

    def __post_init__(self) -> None:
        if type(self.source) is not PlanSource:
            raise ValueError("collected test source requires a PlanSource")
        if self.changed_paths is None:
            if self.source.head_commit is not None:
                raise ValueError("range source requires complete changed paths")
            return
        if self.source.head_commit is None:
            raise ValueError("revision-only source cannot carry changed paths")
        if type(self.changed_paths) is not tuple or len(self.changed_paths) > MAX_CHANGED_PATHS:
            raise ValueError(f"collected test source permits at most {MAX_CHANGED_PATHS} paths")
        total = 0
        for path in self.changed_paths:
            validate_path(path)
            total += len(path.encode("utf-8")) + 1
            if total > MAX_CHANGED_PATH_BYTES_TOTAL:
                raise ValueError("collected changed paths exceed the hard byte limit")
        if self.changed_paths != tuple(sorted(set(self.changed_paths))):
            raise ValueError("collected changed paths must be unique and sorted")


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in _LOWER_HEX for character in value)
    )
