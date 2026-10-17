"""Immutable pure selection results, without Git or execution claims."""

from __future__ import annotations

from dataclasses import dataclass

from yaga.test_planning.models import SelectionMode, Suite


@dataclass(frozen=True, slots=True)
class SelectedSuite:
    """One selected catalog suite and every required variant."""

    suite: str
    variants: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectionReason:
    """A selection cause; irrelevant fields are explicitly absent."""

    code: str
    suite: str | None = None
    rule: str | None = None
    path: str | None = None
    required_by: str | None = None

    def sort_key(self) -> tuple[str, str, str, str, str]:
        """Order nullable fields before their nonempty validated string values."""
        return (
            self.code,
            self.suite or "",
            self.rule or "",
            self.path or "",
            self.required_by or "",
        )


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    """Complete bounded selection, not evidence that any work ran or passed."""

    profile: str
    requested_mode: SelectionMode
    effective_mode: SelectionMode
    changed_paths: tuple[str, ...] | None
    inventory: tuple[Suite, ...]
    selected: tuple[SelectedSuite, ...]
    reasons: tuple[SelectionReason, ...]
