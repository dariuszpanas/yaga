"""Immutable models and invariants for committed Git-mode policy."""

from __future__ import annotations

import unicodedata
from bisect import bisect_left
from dataclasses import InitVar, dataclass, field
from enum import StrEnum
from pathlib import Path

MAX_MODE_POLICY_OVERRIDES = 128
MAX_MODE_ENTRIES = 50_000
MAX_MODE_PATH_BYTES = 4_096
MAX_MODE_PATH_COMPONENTS = 64
MAX_MODE_REVISION_CHARS = 512
MAX_MODE_DIAGNOSTICS = 256
MODE_DISALLOWED_CODE = "mode.disallowed"

_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset("0123456789abcdef")


class ModeKind(StrEnum):
    """One closed portable interpretation of a committed Git leaf mode."""

    REGULAR = "regular"
    EXECUTABLE = "executable"
    SYMLINK = "symlink"
    GITLINK = "gitlink"


MODE_KINDS = (
    ModeKind.REGULAR,
    ModeKind.EXECUTABLE,
    ModeKind.SYMLINK,
    ModeKind.GITLINK,
)

_MODE_PAIR_TO_KIND = {
    ("100644", "blob"): ModeKind.REGULAR,
    ("100755", "blob"): ModeKind.EXECUTABLE,
    ("120000", "blob"): ModeKind.SYMLINK,
    ("160000", "commit"): ModeKind.GITLINK,
}
_KIND_TO_MODE = {kind: mode for (mode, _object_type), kind in _MODE_PAIR_TO_KIND.items()}


@dataclass(frozen=True, slots=True)
class ModePathOverride:
    """One ordered first-match replacement for the default allowed modes."""

    pattern: str
    allowed_modes: tuple[ModeKind, ...]

    def __post_init__(self) -> None:
        from yaga.modes.patterns import validate_mode_pattern

        validate_mode_pattern(self.pattern)
        object.__setattr__(
            self,
            "allowed_modes",
            _validate_allowed_modes(self.allowed_modes, label="path override allowed modes"),
        )


@dataclass(frozen=True, slots=True)
class ModePolicy:
    """One explicit versioned committed Git-mode policy."""

    mode_policy_version: int
    default_allowed_modes: tuple[ModeKind, ...]
    path_overrides: tuple[ModePathOverride, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.mode_policy_version, bool)
            or not isinstance(self.mode_policy_version, int)
            or self.mode_policy_version != 1
        ):
            raise ValueError("mode policy version must be 1")
        object.__setattr__(
            self,
            "default_allowed_modes",
            _validate_allowed_modes(
                self.default_allowed_modes,
                label="default allowed modes",
            ),
        )
        if (
            type(self.path_overrides) is not tuple
            or len(self.path_overrides) > MAX_MODE_POLICY_OVERRIDES
            or any(not isinstance(item, ModePathOverride) for item in self.path_overrides)
        ):
            raise ValueError(
                f"mode policy path overrides must be a tuple of at most "
                f"{MAX_MODE_POLICY_OVERRIDES} ModePathOverride values"
            )
        patterns = tuple(item.pattern for item in self.path_overrides)
        if len(set(patterns)) != len(patterns):
            raise ValueError("mode policy path-override patterns must be unique and case-sensitive")


@dataclass(frozen=True, slots=True)
class LoadedModePolicy:
    """A parsed mode policy together with its explicit resolved path."""

    policy: ModePolicy
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ModePolicy):
            raise ValueError("loaded mode policy requires a ModePolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded mode policy path must be absolute")


@dataclass(frozen=True, slots=True)
class ModeEntry:
    """One canonical leaf entry and its exact committed Git mode."""

    path: str
    oid: str
    mode: str
    object_type: str

    def __post_init__(self) -> None:
        _validate_mode_path(self.path)
        if not _is_object_id(self.oid):
            raise ValueError("mode entry requires a lowercase Git object ID")
        if not isinstance(self.mode, str) or not isinstance(self.object_type, str):
            raise ValueError("mode entry mode and object type must be strings")
        if (self.mode, self.object_type) not in _MODE_PAIR_TO_KIND:
            raise ValueError("mode entry requires one exact supported Git mode/object-type pair")

    @property
    def kind(self) -> ModeKind:
        """Return the closed policy kind derived from the exact Git pair."""
        return _MODE_PAIR_TO_KIND[(self.mode, self.object_type)]


@dataclass(frozen=True, slots=True)
class ModeSelection:
    """One exact committed tree and its canonical leaf entries."""

    repository: Path
    revision: str
    commit_sha: str
    tree_sha: str
    entries: tuple[ModeEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path) or not self.repository.is_absolute():
            raise ValueError("mode selection repository must be absolute")
        _validate_mode_revision(self.revision)
        if (
            not _is_object_id(self.commit_sha)
            or not _is_object_id(self.tree_sha)
            or len(self.commit_sha) != len(self.tree_sha)
        ):
            raise ValueError("mode selection requires same-format lowercase object IDs")
        if (
            type(self.entries) is not tuple
            or len(self.entries) > MAX_MODE_ENTRIES
            or any(not isinstance(item, ModeEntry) for item in self.entries)
        ):
            raise ValueError(
                f"mode selection entries must be a tuple of at most {MAX_MODE_ENTRIES} ModeEntry values"
            )
        paths = tuple(item.path for item in self.entries)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("mode selection entry paths must be unique and sorted")
        if any(len(item.oid) != len(self.commit_sha) for item in self.entries):
            raise ValueError("mode selection entry IDs must use the repository object format")
        for path in paths:
            prefix = f"{path}/"
            candidate_index = bisect_left(paths, prefix)
            if candidate_index < len(paths) and paths[candidate_index].startswith(prefix):
                raise ValueError("mode selection leaf paths cannot contain another leaf path")


@dataclass(frozen=True, slots=True)
class ModeDiagnostic:
    """One stable finding for a committed entry whose kind is disallowed."""

    code: str
    message: str
    path: str
    actual_mode: str
    actual_kind: ModeKind
    allowed_modes: tuple[ModeKind, ...]
    pattern: str | None = None

    def __post_init__(self) -> None:
        if self.code != MODE_DISALLOWED_CODE:
            raise ValueError("mode diagnostic code is not recognized")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("mode diagnostic message must be nonempty")
        _validate_mode_path(self.path)
        if not isinstance(self.actual_kind, ModeKind):
            raise ValueError("mode diagnostic actual kind is not recognized")
        if self.actual_mode != _KIND_TO_MODE[self.actual_kind]:
            raise ValueError("mode diagnostic actual mode does not match its kind")
        object.__setattr__(
            self,
            "allowed_modes",
            _validate_allowed_modes(
                self.allowed_modes,
                label="diagnostic allowed modes",
            ),
        )
        if self.actual_kind in self.allowed_modes:
            raise ValueError("mode diagnostic actual kind must be disallowed")
        if self.pattern is not None:
            from yaga.modes.patterns import validate_mode_pattern

            validate_mode_pattern(self.pattern)


@dataclass(frozen=True, slots=True)
class _ModeDecision:
    """One cached first-match policy decision for a selected path."""

    path: str
    allowed_modes: tuple[ModeKind, ...]
    pattern: str | None


@dataclass(frozen=True, slots=True)
class _ModeEvaluation:
    """One complete bounded mode-policy evaluation."""

    policy: ModePolicy
    selection: ModeSelection
    diagnostics: tuple[ModeDiagnostic, ...]
    finding_count: int
    decisions: tuple[_ModeDecision, ...]


_EVALUATION_SEAL = object()


@dataclass(frozen=True, slots=True)
class ModeReport:
    """The deterministic result of checking one exact committed tree."""

    policy: ModePolicy
    selection: ModeSelection
    diagnostics: tuple[ModeDiagnostic, ...]
    _trusted_evaluation: InitVar[tuple[object, _ModeEvaluation] | None] = field(
        default=None,
        kw_only=True,
    )
    _decisions: tuple[_ModeDecision, ...] = field(init=False, repr=False, compare=False)
    _finding_count: int = field(init=False, repr=False, compare=False)

    def __post_init__(
        self,
        _trusted_evaluation: tuple[object, _ModeEvaluation] | None,
    ) -> None:
        if not isinstance(self.policy, ModePolicy):
            raise ValueError("mode report requires a ModePolicy")
        if not isinstance(self.selection, ModeSelection):
            raise ValueError("mode report requires a ModeSelection")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) > MAX_MODE_DIAGNOSTICS
            or any(not isinstance(item, ModeDiagnostic) for item in self.diagnostics)
        ):
            raise ValueError("mode report requires bounded diagnostics")
        diagnostic_paths = tuple(item.path for item in self.diagnostics)
        if diagnostic_paths != tuple(sorted(diagnostic_paths)) or len(set(diagnostic_paths)) != len(
            diagnostic_paths
        ):
            raise ValueError("mode report diagnostics must have unique lexical paths")
        entries_by_path = {item.path: item for item in self.selection.entries}
        if any(
            diagnostic.path not in entries_by_path
            or diagnostic.actual_mode != entries_by_path[diagnostic.path].mode
            or diagnostic.actual_kind is not entries_by_path[diagnostic.path].kind
            for diagnostic in self.diagnostics
        ):
            raise ValueError("mode report diagnostics must identify selected entry modes")

        if _trusted_evaluation is None:
            from yaga.modes.patterns import ModeMatchWorkLimitError

            try:
                evaluation = _evaluate_mode_policy(self.policy, self.selection)
            except ModeMatchWorkLimitError as error:
                raise ValueError(
                    "mode report validation exceeds the hard match-work limit"
                ) from error
        else:
            seal, evaluation = _trusted_evaluation
            if seal is not _EVALUATION_SEAL or not isinstance(evaluation, _ModeEvaluation):
                raise ValueError("mode report received an invalid trusted evaluation")
            if evaluation.policy is not self.policy or evaluation.selection is not self.selection:
                raise ValueError("mode report evaluation identity does not match the report")

        if self.diagnostics != evaluation.diagnostics:
            raise ValueError(
                "mode report diagnostics must be the complete exact ordered policy result"
            )
        if not 0 <= evaluation.finding_count <= len(self.selection.entries) or len(
            evaluation.diagnostics
        ) != min(evaluation.finding_count, MAX_MODE_DIAGNOSTICS):
            raise ValueError("mode report evaluation has inconsistent exact finding counts")
        if len(evaluation.decisions) != len(self.selection.entries) or tuple(
            item.path for item in evaluation.decisions
        ) != tuple(item.path for item in self.selection.entries):
            raise ValueError("mode report decisions do not match selected entries")
        object.__setattr__(self, "_decisions", evaluation.decisions)
        object.__setattr__(self, "_finding_count", evaluation.finding_count)

    @classmethod
    def _from_evaluation(cls, evaluation: _ModeEvaluation) -> ModeReport:
        """Construct a report from one private complete bounded evaluation."""
        if not isinstance(evaluation, _ModeEvaluation):
            raise TypeError("evaluation must be a _ModeEvaluation")
        return cls(
            policy=evaluation.policy,
            selection=evaluation.selection,
            diagnostics=evaluation.diagnostics,
            _trusted_evaluation=(_EVALUATION_SEAL, evaluation),
        )

    @property
    def status(self) -> str:
        """Return the stable aggregate report status."""
        return "failed" if self.diagnostics else "passed"

    @property
    def valid(self) -> bool:
        """Return whether every committed entry kind satisfies policy."""
        return not self.diagnostics

    @property
    def finding_count(self) -> int:
        """Return the exact number of disallowed committed entries."""
        return self._finding_count

    @property
    def diagnostics_omitted(self) -> int:
        """Return the exact number of findings omitted from stored diagnostics."""
        return self.finding_count - len(self.diagnostics)

    @property
    def counts_by_kind(self) -> dict[ModeKind, int]:
        """Return exact selected-entry counts in canonical kind order."""
        counts = dict.fromkeys(MODE_KINDS, 0)
        for entry in self.selection.entries:
            counts[entry.kind] += 1
        return counts

    @property
    def entries_by_mode(self) -> dict[str, int]:
        """Return exact selected-entry counts keyed by canonical policy spelling."""
        return {kind.value: count for kind, count in self.counts_by_kind.items()}

    @property
    def disallowed_entries(self) -> tuple[ModeDiagnostic, ...]:
        """Return the stored bounded finding prefix in lexical path order."""
        return self.diagnostics

    def _selected_modes_for_path(
        self,
        path: str,
    ) -> tuple[tuple[ModeKind, ...], str | None]:
        """Return the cached first-match policy decision for one selected path."""
        index = bisect_left(self._decisions, path, key=lambda decision: decision.path)
        if index >= len(self._decisions) or self._decisions[index].path != path:
            raise ValueError("path does not identify an entry in this mode report")
        decision = self._decisions[index]
        return decision.allowed_modes, decision.pattern


def _validate_allowed_modes(
    value: object,
    *,
    label: str,
) -> tuple[ModeKind, ...]:
    if (
        type(value) is not tuple
        or not 1 <= len(value) <= len(MODE_KINDS)
        or any(not isinstance(item, ModeKind) for item in value)
    ):
        raise ValueError(f"mode policy {label} must be a nonempty tuple of ModeKind values")
    if len(set(value)) != len(value):
        raise ValueError(f"mode policy {label} must be unique")
    return tuple(kind for kind in MODE_KINDS if kind in value)


def _validate_mode_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_MODE_REVISION_CHARS
        or value.startswith(("-", "^"))
        or ":" in value
        or ".." in value
        or any(selector in value for selector in ("^@", "^!", "^-"))
        or any(character.isspace() or _unsafe_revision_character(character) for character in value)
    ):
        raise ValueError(
            f"mode revision must be a safe non-option string of at most "
            f"{MAX_MODE_REVISION_CHARS} characters"
        )
    return value


def _validate_mode_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise ValueError("mode path must be a canonical repository-relative UTF-8 leaf path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("mode path must be valid UTF-8") from error
    components = tuple(value.split("/"))
    if (
        len(encoded) > MAX_MODE_PATH_BYTES
        or not 1 <= len(components) <= MAX_MODE_PATH_COMPONENTS
        or value.startswith("/")
        or "\x00" in value
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError("mode path must be a canonical repository-relative UTF-8 leaf path")
    return components


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in _OBJECT_ID_LENGTHS
        and all(character in _LOWER_HEX for character in value)
    )


def _unsafe_revision_character(value: str) -> bool:
    return unicodedata.category(value) in {"Cc", "Cf", "Cs", "Zl", "Zp"}


def _evaluate_mode_policy(
    policy: ModePolicy,
    selection: ModeSelection,
    *,
    match_work_limit: int | None = None,
) -> _ModeEvaluation:
    """Evaluate one policy once and cache every first-match decision."""
    from yaga.modes.patterns import (
        MAX_MODE_MATCH_WORK,
        _compile_mode_components,
        _match_compiled_mode_components,
        _MatchWork,
        validate_mode_pattern,
    )

    compiled_overrides = tuple(
        (
            override,
            _compile_mode_components(validate_mode_pattern(override.pattern)),
        )
        for override in policy.path_overrides
    )
    match_work = _MatchWork(MAX_MODE_MATCH_WORK if match_work_limit is None else match_work_limit)
    diagnostics: list[ModeDiagnostic] = []
    finding_count = 0
    decisions: list[_ModeDecision] = []
    for entry in selection.entries:
        selected: ModePathOverride | None = None
        components = tuple(entry.path.split("/"))
        for override, compiled_pattern in compiled_overrides:
            if _match_compiled_mode_components(
                compiled_pattern,
                components,
                spend_work=match_work.spend,
            ):
                selected = override
                break
        allowed_modes = (
            selected.allowed_modes if selected is not None else policy.default_allowed_modes
        )
        pattern = selected.pattern if selected is not None else None
        decisions.append(_ModeDecision(entry.path, allowed_modes, pattern))
        if entry.kind not in allowed_modes:
            finding_count += 1
            if len(diagnostics) < MAX_MODE_DIAGNOSTICS:
                diagnostics.append(
                    ModeDiagnostic(
                        code=MODE_DISALLOWED_CODE,
                        message="committed entry mode is not allowed by policy",
                        path=entry.path,
                        actual_mode=entry.mode,
                        actual_kind=entry.kind,
                        allowed_modes=allowed_modes,
                        pattern=pattern,
                    )
                )
    return _ModeEvaluation(
        policy=policy,
        selection=selection,
        diagnostics=tuple(diagnostics),
        finding_count=finding_count,
        decisions=tuple(decisions),
    )
