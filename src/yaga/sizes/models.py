"""Immutable models and invariants for committed blob-size policy."""

from __future__ import annotations

import unicodedata
from bisect import bisect_left
from dataclasses import InitVar, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_SIZE_POLICY_ENTRIES = 128
MAX_SIZE_ENTRIES = 50_000
MAX_SIZE_PATH_BYTES = 4_096
MAX_SIZE_PATH_COMPONENTS = 64
MAX_SIZE_REVISION_CHARS = 512
MAX_SIZE_BYTES = (1 << 53) - 1
MAX_SIZE_TOTAL_BYTES = MAX_SIZE_BYTES
SIZE_BLOB_CODE = "size.blob"
SIZE_TOTAL_CODE = "size.total"

_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset("0123456789abcdef")
_BLOB_MODES = frozenset({"100644", "100755", "120000"})
_SIZE_DIAGNOSTIC_CODES = frozenset({SIZE_BLOB_CODE, SIZE_TOTAL_CODE})


class SizeTotalBytesLimitError(RuntimeError):
    """Private operational signal that aggregate bytes exceed exact JSON range."""


@dataclass(frozen=True, slots=True)
class SizePathLimit:
    """One ordered path-specific committed-blob limit."""

    pattern: str
    max_blob_bytes: int

    def __post_init__(self) -> None:
        from yaga.sizes.patterns import validate_size_pattern

        validate_size_pattern(self.pattern)
        _validate_byte_integer(self.max_blob_bytes, label="path max blob bytes")


@dataclass(frozen=True, slots=True)
class SizePolicy:
    """One explicit versioned committed blob-size policy."""

    size_policy_version: int
    default_max_blob_bytes: int
    max_total_blob_bytes: int | None = None
    path_limits: tuple[SizePathLimit, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.size_policy_version, bool)
            or not isinstance(self.size_policy_version, int)
            or self.size_policy_version != 1
        ):
            raise ValueError("size policy version must be 1")
        _validate_byte_integer(self.default_max_blob_bytes, label="default max blob bytes")
        if self.max_total_blob_bytes is not None:
            _validate_byte_integer(self.max_total_blob_bytes, label="max total blob bytes")
        if (
            type(self.path_limits) is not tuple
            or len(self.path_limits) > MAX_SIZE_POLICY_ENTRIES
            or any(not isinstance(item, SizePathLimit) for item in self.path_limits)
        ):
            raise ValueError(
                f"size policy path limits must be a tuple of at most "
                f"{MAX_SIZE_POLICY_ENTRIES} SizePathLimit values"
            )
        patterns = tuple(item.pattern for item in self.path_limits)
        if len(set(patterns)) != len(patterns):
            raise ValueError("size policy path-limit patterns must be unique and case-sensitive")


@dataclass(frozen=True, slots=True)
class LoadedSizePolicy:
    """A parsed size policy together with its explicit resolved path."""

    policy: SizePolicy
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SizePolicy):
            raise ValueError("loaded size policy requires a SizePolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded size policy path must be absolute")


@dataclass(frozen=True, slots=True)
class BlobEntry:
    """One canonical leaf blob in an exact committed tree."""

    path: str
    oid: str
    mode: str
    size: int

    def __post_init__(self) -> None:
        _validate_size_path(self.path)
        if not _is_object_id(self.oid):
            raise ValueError("blob entry requires a lowercase Git object ID")
        if not isinstance(self.mode, str) or self.mode not in _BLOB_MODES:
            raise ValueError("blob entry mode must be 100644, 100755, or 120000")
        _validate_byte_integer(self.size, label="blob size")


@dataclass(frozen=True, slots=True)
class SizeSelection:
    """One exact committed tree and its canonical blobs and gitlinks."""

    repository: Path
    revision: str
    commit_sha: str
    tree_sha: str
    blobs: tuple[BlobEntry, ...]
    gitlinks: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path) or not self.repository.is_absolute():
            raise ValueError("size selection repository must be absolute")
        _validate_size_revision(self.revision)
        if (
            not _is_object_id(self.commit_sha)
            or not _is_object_id(self.tree_sha)
            or len(self.commit_sha) != len(self.tree_sha)
        ):
            raise ValueError("size selection requires same-format lowercase object IDs")
        if type(self.blobs) is not tuple or any(
            not isinstance(item, BlobEntry) for item in self.blobs
        ):
            raise ValueError("size selection blobs must be a tuple of BlobEntry values")
        if type(self.gitlinks) is not tuple or any(
            not isinstance(path, str) for path in self.gitlinks
        ):
            raise ValueError("size selection gitlinks must be a tuple of paths")
        if len(self.blobs) + len(self.gitlinks) > MAX_SIZE_ENTRIES:
            raise ValueError(f"size selection exceeds {MAX_SIZE_ENTRIES} entries")

        blob_paths = tuple(item.path for item in self.blobs)
        if blob_paths != tuple(sorted(blob_paths)) or len(set(blob_paths)) != len(blob_paths):
            raise ValueError("size selection blob paths must be unique and sorted")
        if any(len(item.oid) != len(self.commit_sha) for item in self.blobs):
            raise ValueError("size selection blob IDs must use the repository object format")
        sizes_by_oid: dict[str, int] = {}
        for blob in self.blobs:
            previous_size = sizes_by_oid.setdefault(blob.oid, blob.size)
            if previous_size != blob.size:
                raise ValueError("size selection repeats one blob ID with inconsistent sizes")
        for path in self.gitlinks:
            _validate_size_path(path)
        if self.gitlinks != tuple(sorted(self.gitlinks)) or len(set(self.gitlinks)) != len(
            self.gitlinks
        ):
            raise ValueError("size selection gitlinks must be unique and sorted")
        if not set(blob_paths).isdisjoint(self.gitlinks):
            raise ValueError("size selection blob and gitlink paths must be disjoint")
        leaf_paths = tuple(sorted((*blob_paths, *self.gitlinks)))
        for path in leaf_paths:
            prefix = f"{path}/"
            candidate_index = bisect_left(leaf_paths, prefix)
            if candidate_index < len(leaf_paths) and leaf_paths[candidate_index].startswith(prefix):
                raise ValueError("size selection leaf paths cannot contain another leaf path")


@dataclass(frozen=True, slots=True)
class SizeDiagnostic:
    """One stable per-blob or aggregate size-policy finding."""

    code: str
    message: str
    size: int
    limit: int
    path: str | None = None
    pattern: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in _SIZE_DIAGNOSTIC_CODES:
            raise ValueError("size diagnostic code is not recognized")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("size diagnostic message must be nonempty")
        _validate_byte_integer(self.size, label="diagnostic size")
        _validate_byte_integer(self.limit, label="diagnostic limit")
        if self.size <= self.limit:
            raise ValueError("size diagnostic requires a size greater than its limit")
        if self.code == SIZE_BLOB_CODE:
            _validate_size_path(self.path)
            if self.pattern is not None:
                from yaga.sizes.patterns import validate_size_pattern

                validate_size_pattern(self.pattern)
        elif self.path is not None or self.pattern is not None:
            raise ValueError("aggregate size diagnostic cannot carry a path or pattern")


@dataclass(frozen=True, slots=True)
class _SizeDecision:
    """One cached first-match decision aligned with a selected blob path."""

    path: str
    max_blob_bytes: int
    pattern: str | None


@dataclass(frozen=True, slots=True)
class _SizeEvaluation:
    """One complete bounded evaluation used to construct an exact report."""

    policy: SizePolicy
    selection: SizeSelection
    diagnostics: tuple[SizeDiagnostic, ...]
    decisions: tuple[_SizeDecision, ...]


_EVALUATION_SEAL = object()


@dataclass(frozen=True, slots=True)
class SizeReport:
    """The deterministic result of checking one exact committed tree."""

    policy: SizePolicy
    selection: SizeSelection
    diagnostics: tuple[SizeDiagnostic, ...]
    _trusted_evaluation: InitVar[tuple[object, _SizeEvaluation] | None] = field(
        default=None,
        kw_only=True,
    )
    _decisions: tuple[_SizeDecision, ...] = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(
        self,
        _trusted_evaluation: tuple[object, _SizeEvaluation] | None,
    ) -> None:
        if not isinstance(self.policy, SizePolicy):
            raise ValueError("size report requires a SizePolicy")
        if not isinstance(self.selection, SizeSelection):
            raise ValueError("size report requires a SizeSelection")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) > len(self.selection.blobs) + 1
            or any(not isinstance(item, SizeDiagnostic) for item in self.diagnostics)
        ):
            raise ValueError("size report requires bounded diagnostics")

        blob_diagnostics: list[SizeDiagnostic] = []
        aggregate: SizeDiagnostic | None = None
        for diagnostic in self.diagnostics:
            if diagnostic.code == SIZE_BLOB_CODE:
                if aggregate is not None:
                    raise ValueError("per-blob size diagnostics must precede aggregate diagnostics")
                blob_diagnostics.append(diagnostic)
            else:
                if aggregate is not None:
                    raise ValueError("size report permits at most one aggregate diagnostic")
                aggregate = diagnostic

        diagnostic_paths = tuple(item.path for item in blob_diagnostics if item.path is not None)
        if diagnostic_paths != tuple(sorted(diagnostic_paths)) or len(set(diagnostic_paths)) != len(
            diagnostic_paths
        ):
            raise ValueError("per-blob size diagnostics must have unique lexical paths")
        blobs_by_path = {item.path: item for item in self.selection.blobs}
        if any(
            diagnostic.path not in blobs_by_path
            or diagnostic.size != blobs_by_path[diagnostic.path].size
            for diagnostic in blob_diagnostics
        ):
            raise ValueError("per-blob size diagnostics must identify selected blob sizes")
        configured_limits = {item.pattern: item.max_blob_bytes for item in self.policy.path_limits}
        if any(
            (diagnostic.pattern is None and diagnostic.limit != self.policy.default_max_blob_bytes)
            or (
                diagnostic.pattern is not None
                and configured_limits.get(diagnostic.pattern) != diagnostic.limit
            )
            for diagnostic in blob_diagnostics
        ):
            raise ValueError("per-blob size diagnostics must identify configured limits")

        total_bytes = sum(item.size for item in self.selection.blobs)
        expected_aggregate = (
            self.policy.max_total_blob_bytes is not None
            and total_bytes > self.policy.max_total_blob_bytes
        )
        if (aggregate is not None) != expected_aggregate or (
            aggregate is not None
            and (
                aggregate.size != total_bytes or aggregate.limit != self.policy.max_total_blob_bytes
            )
        ):
            raise ValueError("aggregate size diagnostic does not match policy and selection")

        if _trusted_evaluation is None:
            from yaga.sizes.patterns import SizeMatchWorkLimitError

            try:
                evaluation = _evaluate_size_policy(self.policy, self.selection)
            except (SizeMatchWorkLimitError, SizeTotalBytesLimitError) as error:
                raise ValueError(
                    "size report validation exceeds a hard evaluation limit"
                ) from error
        else:
            seal, evaluation = _trusted_evaluation
            if seal is not _EVALUATION_SEAL or not isinstance(evaluation, _SizeEvaluation):
                raise ValueError("size report received an invalid trusted evaluation")
            if evaluation.policy is not self.policy or evaluation.selection is not self.selection:
                raise ValueError("size report evaluation identity does not match the report")

        if self.diagnostics != evaluation.diagnostics:
            raise ValueError(
                "size report diagnostics must be the complete exact ordered policy result"
            )
        if len(evaluation.decisions) != len(self.selection.blobs) or tuple(
            item.path for item in evaluation.decisions
        ) != tuple(item.path for item in self.selection.blobs):
            raise ValueError("size report evaluation decisions do not match selected blobs")
        object.__setattr__(self, "_decisions", evaluation.decisions)

    @classmethod
    def _from_evaluation(cls, evaluation: _SizeEvaluation) -> SizeReport:
        """Construct a report from one private complete bounded evaluation."""
        if not isinstance(evaluation, _SizeEvaluation):
            raise TypeError("evaluation must be a _SizeEvaluation")
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
        """Return whether the committed blobs satisfy policy."""
        return not self.diagnostics

    @property
    def total_bytes(self) -> int:
        """Return logical checkout bytes, counting each selected path once."""
        return sum(item.size for item in self.selection.blobs)

    @property
    def oversized_blobs(self) -> tuple[SizeDiagnostic, ...]:
        """Return per-blob findings in lexical path order."""
        return tuple(item for item in self.diagnostics if item.code == SIZE_BLOB_CODE)

    @property
    def total_exceeded(self) -> bool:
        """Return whether the configured aggregate limit was exceeded."""
        return any(item.code == SIZE_TOTAL_CODE for item in self.diagnostics)

    def _selected_limit_for_path(self, path: str) -> tuple[int, str | None]:
        """Return the cached first-match limit for one selected blob path."""
        index = bisect_left(self._decisions, path, key=lambda decision: decision.path)
        if index >= len(self._decisions) or self._decisions[index].path != path:
            raise ValueError("path does not identify a blob in this size report")
        decision = self._decisions[index]
        return decision.max_blob_bytes, decision.pattern


def _validate_byte_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SIZE_BYTES:
        raise ValueError(f"{label} must be an integer from 0 through {MAX_SIZE_BYTES}")
    return value


def _validate_size_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SIZE_REVISION_CHARS
        or value.startswith(("-", "^"))
        or ".." in value
        or any(selector in value for selector in ("^@", "^!", "^-"))
        or any(character.isspace() or _unsafe_character(character) for character in value)
    ):
        raise ValueError(
            f"size revision must be a safe non-option string of at most "
            f"{MAX_SIZE_REVISION_CHARS} characters"
        )
    return value


def _validate_size_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("size path must be a canonical repository-relative POSIX leaf path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("size path must be valid UTF-8") from error
    path = PurePosixPath(value)
    components = path.parts
    if (
        len(encoded) > MAX_SIZE_PATH_BYTES
        or not 1 <= len(components) <= MAX_SIZE_PATH_COMPONENTS
        or path.is_absolute()
        or bool(PureWindowsPath(value).drive)
        or path.as_posix() != value
        or any(component in {"", ".", ".."} for component in components)
        or any(_unsafe_character(character) for character in value)
    ):
        raise ValueError("size path must be a canonical repository-relative POSIX leaf path")
    return components


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in _OBJECT_ID_LENGTHS
        and all(character in _LOWER_HEX for character in value)
    )


def _unsafe_character(value: str) -> bool:
    return unicodedata.category(value) in {"Cc", "Cf", "Cs", "Zl", "Zp"}


def _evaluate_size_policy(
    policy: SizePolicy,
    selection: SizeSelection,
    *,
    match_work_limit: int | None = None,
) -> _SizeEvaluation:
    """Evaluate one policy once and cache every first-match decision."""
    from yaga.sizes.patterns import (
        MAX_SIZE_MATCH_WORK,
        _compile_size_components,
        _match_compiled_size_components,
        _MatchWork,
        validate_size_pattern,
    )

    total_bytes = 0
    for blob in selection.blobs:
        if blob.size > MAX_SIZE_TOTAL_BYTES - total_bytes:
            raise SizeTotalBytesLimitError
        total_bytes += blob.size

    compiled_limits = tuple(
        (
            path_limit,
            _compile_size_components(validate_size_pattern(path_limit.pattern)),
        )
        for path_limit in policy.path_limits
    )
    match_work = _MatchWork(MAX_SIZE_MATCH_WORK if match_work_limit is None else match_work_limit)
    diagnostics: list[SizeDiagnostic] = []
    decisions: list[_SizeDecision] = []
    for blob in selection.blobs:
        selected: SizePathLimit | None = None
        components = tuple(blob.path.split("/"))
        for path_limit, compiled_pattern in compiled_limits:
            if _match_compiled_size_components(
                compiled_pattern,
                components,
                spend_work=match_work.spend,
            ):
                selected = path_limit
                break
        limit = selected.max_blob_bytes if selected is not None else policy.default_max_blob_bytes
        pattern = selected.pattern if selected is not None else None
        decisions.append(
            _SizeDecision(
                path=blob.path,
                max_blob_bytes=limit,
                pattern=pattern,
            )
        )
        if blob.size > limit:
            diagnostics.append(
                SizeDiagnostic(
                    code=SIZE_BLOB_CODE,
                    message="committed blob exceeds its configured byte limit",
                    path=blob.path,
                    size=blob.size,
                    limit=limit,
                    pattern=pattern,
                )
            )

    if policy.max_total_blob_bytes is not None and total_bytes > policy.max_total_blob_bytes:
        diagnostics.append(
            SizeDiagnostic(
                code=SIZE_TOTAL_CODE,
                message="total stored blob bytes exceed the configured limit",
                size=total_bytes,
                limit=policy.max_total_blob_bytes,
            )
        )
    return _SizeEvaluation(
        policy=policy,
        selection=selection,
        diagnostics=tuple(diagnostics),
        decisions=tuple(decisions),
    )
