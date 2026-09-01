"""Immutable models and invariants for tracked-tree policy."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_TREE_POLICY_ENTRIES = 128
MAX_TREE_PATHS = 50_000
MAX_TREE_PATH_BYTES = 4_096
MAX_TREE_PATH_COMPONENTS = 64
MAX_TREE_REVISION_CHARS = 512
TREE_REQUIRED_CODE = "tree.required"
TREE_FORBIDDEN_CODE = "tree.forbidden"

_OBJECT_ID_LENGTHS = frozenset({40, 64})
_LOWER_HEX = frozenset("0123456789abcdef")
_TREE_DIAGNOSTIC_CODES = frozenset({TREE_REQUIRED_CODE, TREE_FORBIDDEN_CODE})


@dataclass(frozen=True, slots=True)
class TreePolicy:
    """One explicit versioned tracked-tree policy."""

    tree_policy_version: int
    required_paths: tuple[str, ...]
    forbidden_patterns: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.tree_policy_version, bool)
            or not isinstance(self.tree_policy_version, int)
            or self.tree_policy_version != 1
        ):
            raise ValueError("tree policy version must be 1")
        _validate_policy_values(self.required_paths, label="required paths")
        _validate_policy_values(self.forbidden_patterns, label="forbidden patterns")
        total = len(self.required_paths) + len(self.forbidden_patterns)
        if not 1 <= total <= MAX_TREE_POLICY_ENTRIES:
            raise ValueError(
                f"tree policy requires 1 through {MAX_TREE_POLICY_ENTRIES} combined path entries"
            )

        for path in self.required_paths:
            _validate_tree_path(path)

        from yaga.trees.patterns import (
            TreeMatchWorkLimitError,
            _compile_tree_components,
            _match_compiled_tree_components,
            _MatchWork,
            validate_tree_pattern,
        )

        compiled = tuple(
            (
                pattern,
                _compile_tree_components(validate_tree_pattern(pattern)),
            )
            for pattern in self.forbidden_patterns
        )
        work = _MatchWork()
        try:
            for path in self.required_paths:
                components = tuple(path.split("/"))
                for pattern, compiled_pattern in compiled:
                    if _match_compiled_tree_components(
                        compiled_pattern,
                        components,
                        spend_work=work.spend,
                    ):
                        raise ValueError(
                            f"required path {path!r} is forbidden by pattern {pattern!r}"
                        )
        except TreeMatchWorkLimitError as error:
            raise ValueError(
                f"tree policy conflict analysis exceeds the hard {work.limit}-unit match-work limit"
            ) from error


@dataclass(frozen=True, slots=True)
class LoadedTreePolicy:
    """A parsed tree policy together with its explicit resolved path."""

    policy: TreePolicy
    path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.policy, TreePolicy):
            raise ValueError("loaded tree policy requires a TreePolicy")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("loaded tree policy path must be absolute")


@dataclass(frozen=True, slots=True)
class TreeSelection:
    """One exact Git tree and its canonical tracked leaf paths."""

    repository: Path
    revision: str
    commit_sha: str
    tree_sha: str
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Path) or not self.repository.is_absolute():
            raise ValueError("tree selection repository must be absolute")
        _validate_tree_revision(self.revision)
        if (
            not _is_object_id(self.commit_sha)
            or not _is_object_id(self.tree_sha)
            or len(self.commit_sha) != len(self.tree_sha)
        ):
            raise ValueError("tree selection requires same-format lowercase object IDs")
        if type(self.paths) is not tuple or len(self.paths) > MAX_TREE_PATHS:
            raise ValueError(f"tree selection exceeds {MAX_TREE_PATHS} paths")
        for path in self.paths:
            _validate_tree_path(path)
        if self.paths != tuple(sorted(self.paths)) or len(set(self.paths)) != len(self.paths):
            raise ValueError("tree selection paths must be unique and sorted")


@dataclass(frozen=True, slots=True)
class TreeDiagnostic:
    """One stable missing- or forbidden-path finding."""

    code: str
    message: str
    path: str
    pattern: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or self.code not in _TREE_DIAGNOSTIC_CODES:
            raise ValueError("tree diagnostic code is not recognized")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("tree diagnostic message must be nonempty")
        _validate_tree_path(self.path)
        if self.code == TREE_REQUIRED_CODE:
            if self.pattern is not None:
                raise ValueError("required-path diagnostic cannot carry a pattern")
        elif not isinstance(self.pattern, str) or not self.pattern:
            raise ValueError("forbidden-path diagnostic requires a pattern")


@dataclass(frozen=True, slots=True)
class TreeReport:
    """The deterministic result of checking one exact tracked tree."""

    policy: TreePolicy
    selection: TreeSelection
    diagnostics: tuple[TreeDiagnostic, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy, TreePolicy):
            raise ValueError("tree report requires a TreePolicy")
        if not isinstance(self.selection, TreeSelection):
            raise ValueError("tree report requires a TreeSelection")
        if (
            type(self.diagnostics) is not tuple
            or len(self.diagnostics) > len(self.policy.required_paths) + len(self.selection.paths)
            or any(not isinstance(item, TreeDiagnostic) for item in self.diagnostics)
        ):
            raise ValueError("tree report requires bounded diagnostics")

        selected = set(self.selection.paths)
        missing = tuple(path for path in self.policy.required_paths if path not in selected)
        required_diagnostics = self.diagnostics[: len(missing)]
        if tuple(item.path for item in required_diagnostics) != missing or any(
            item.code != TREE_REQUIRED_CODE or item.pattern is not None
            for item in required_diagnostics
        ):
            raise ValueError(
                "tree report missing-path diagnostics must follow required policy order"
            )

        forbidden_diagnostics = self.diagnostics[len(missing) :]
        forbidden_paths = tuple(item.path for item in forbidden_diagnostics)
        if (
            any(item.code != TREE_FORBIDDEN_CODE for item in forbidden_diagnostics)
            or forbidden_paths != tuple(sorted(forbidden_paths))
            or len(set(forbidden_paths)) != len(forbidden_paths)
            or any(path not in selected for path in forbidden_paths)
            or any(
                item.pattern not in self.policy.forbidden_patterns for item in forbidden_diagnostics
            )
        ):
            raise ValueError(
                "tree report forbidden-path diagnostics must be unique, selected, and sorted"
            )

    @property
    def status(self) -> str:
        """Return the stable aggregate report status."""
        return "failed" if self.diagnostics else "passed"

    @property
    def valid(self) -> bool:
        """Return whether the tracked tree satisfies policy."""
        return not self.diagnostics

    @property
    def missing_required(self) -> tuple[str, ...]:
        """Return missing exact paths in configured order."""
        return tuple(item.path for item in self.diagnostics if item.code == TREE_REQUIRED_CODE)

    @property
    def forbidden_paths(self) -> tuple[str, ...]:
        """Return forbidden tracked paths in lexical order."""
        return tuple(item.path for item in self.diagnostics if item.code == TREE_FORBIDDEN_CODE)


def _validate_policy_values(value: tuple[str, ...], *, label: str) -> None:
    if type(value) is not tuple or any(not isinstance(item, str) for item in value):
        raise ValueError(f"tree policy {label} must be a tuple of strings")
    if len(value) > MAX_TREE_POLICY_ENTRIES:
        raise ValueError(f"tree policy {label} exceeds {MAX_TREE_POLICY_ENTRIES} entries")
    if len(set(value)) != len(value):
        raise ValueError(f"tree policy {label} must be unique")


def _validate_tree_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TREE_REVISION_CHARS
        or value.startswith(("-", "^"))
        or ".." in value
        or any(selector in value for selector in ("^@", "^!", "^-"))
        or any(character.isspace() or _unsafe_character(character) for character in value)
    ):
        raise ValueError(
            f"tree revision must be a safe non-option string of at most "
            f"{MAX_TREE_REVISION_CHARS} characters"
        )
    return value


def _validate_tree_path(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("tree path must be a canonical repository-relative POSIX leaf path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("tree path must be valid UTF-8") from error
    path = PurePosixPath(value)
    components = path.parts
    if (
        len(encoded) > MAX_TREE_PATH_BYTES
        or not 1 <= len(components) <= MAX_TREE_PATH_COMPONENTS
        or path.is_absolute()
        or bool(PureWindowsPath(value).drive)
        or path.as_posix() != value
        or any(component in {"", ".", ".."} for component in components)
        or any(_unsafe_character(character) for character in value)
    ):
        raise ValueError("tree path must be a canonical repository-relative POSIX leaf path")
    return components


def _is_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in _OBJECT_ID_LENGTHS
        and all(character in _LOWER_HEX for character in value)
    )


def _unsafe_character(value: str) -> bool:
    return unicodedata.category(value) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
