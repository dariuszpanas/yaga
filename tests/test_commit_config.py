"""Tests for strict YAGA TOML discovery and commit policy parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.commits.config import MAX_CONFIG_BYTES, load_config
from yaga.commits.models import (
    CasePolicy,
    CommitPolicy,
    EndingPolicy,
    MergePolicy,
    PresencePolicy,
)
from yaga.errors import ConfigurationError


def write_pyproject(path: Path, commit: str = "") -> Path:
    project = path / "pyproject.toml"
    project.write_text(
        "[tool.yaga]\nconfig-version = 1\n\n[tool.yaga.commit]\n" + commit,
        encoding="utf-8",
    )
    return project


def test_missing_configuration_uses_spec_only_defaults(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    loaded = load_config(start=tmp_path)

    assert loaded.path is None
    assert loaded.policy.allowed_types is None
    assert loaded.policy.scope_policy is PresencePolicy.OPTIONAL
    assert loaded.policy.body_min_words == 0
    assert loaded.policy.merge_commits is MergePolicy.IGNORE


def test_commit_policy_preserves_the_legacy_positional_constructor() -> None:
    policy = CommitPolicy(
        1,
        ("feat",),
        CasePolicy.LOWER,
        PresencePolicy.REQUIRED,
        ("cli",),
        CasePolicy.UPPER,
        80,
        3,
        72,
        EndingPolicy.FORBID,
        PresencePolicy.OPTIONAL,
        10,
        100,
        MergePolicy.REJECT,
        ("Revert *",),
        64,
    )

    assert policy.body_min_length == 10
    assert policy.body_max_line_length == 100
    assert policy.merge_commits is MergePolicy.REJECT
    assert policy.ignored_headers == ("Revert *",)
    assert policy.max_commits == 64
    assert policy.body_min_words == 0


def test_nearest_pyproject_is_discovered_from_a_nested_directory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["feat", "fix"]\ntype-case = "lower"\nmax-commits = 12\n',
    )
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)

    loaded = load_config(start=nested)

    assert loaded.path == project
    assert loaded.policy.allowed_types == ("feat", "fix")
    assert loaded.policy.type_case is CasePolicy.LOWER
    assert loaded.policy.max_commits == 12


def test_standalone_configuration_wins_in_the_same_directory(tmp_path: Path) -> None:
    write_pyproject(tmp_path, 'allowed-types = ["feat"]\n')
    standalone = tmp_path / ".yaga.toml"
    standalone.write_text(
        'config-version = 1\n[commit]\nallowed-types = ["docs"]\n',
        encoding="utf-8",
    )

    loaded = load_config(start=tmp_path)

    assert loaded.path == standalone
    assert loaded.policy.allowed_types == ("docs",)


def test_explicit_configuration_does_not_merge_discovered_values(tmp_path: Path) -> None:
    write_pyproject(tmp_path, 'allowed-types = ["feat"]\n')
    explicit = tmp_path / "policy.toml"
    explicit.write_text(
        'config-version = 1\n[commit]\nscope-policy = "required"\n',
        encoding="utf-8",
    )

    loaded = load_config(explicit, start=tmp_path)

    assert loaded.path == explicit.resolve()
    assert loaded.policy.allowed_types is None
    assert loaded.policy.scope_policy is PresencePolicy.REQUIRED


def test_discovery_does_not_escape_the_git_root(tmp_path: Path) -> None:
    write_pyproject(tmp_path, 'allowed-types = ["feat"]\n')
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    nested = repository / "nested"
    nested.mkdir()

    assert load_config(start=nested).path is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[tool.yaga]\nunknown = true\n", "unknown YAGA"),
        ("[tool.yaga]\nconfig-version = 2\n", "config-version"),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nmerge-commits = "sometimes"\n',
            "merge-commits",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nallowed-types = ["feat", "FEAT"]\n',
            "duplicate token",
        ),
        (
            "[tool.yaga]\n[tool.yaga.commit]\nallowed-types = []\n",
            "must not be empty",
        ),
        (
            "[tool.yaga]\n[tool.yaga.commit]\n"
            'scope-policy = "forbidden"\nallowed-scopes = ["cli"]\n',
            "allowed-scopes",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nscope-policy = "required"\nallowed-scopes = []\n',
            "cannot be empty",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nallowed-types = ["feature"]\nheader-max-length = 8\n',
            "minimum header length",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nallowed-scopes = ["   "]\n',
            "invalid token",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nignored-headers = ["\\u009b31m*"]\n',
            "unsafe value",
        ),
    ],
)
def test_invalid_configuration_fails_loudly(tmp_path: Path, content: str, message: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_config(project)


def test_explicit_empty_scope_list_forbids_any_named_scope(tmp_path: Path) -> None:
    project = write_pyproject(tmp_path, "allowed-scopes = []\n")

    loaded = load_config(project)

    assert loaded.policy.allowed_scopes == ()


def test_small_but_satisfiable_length_limits_are_supported(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["f"]\nheader-max-length = 4\nbody-max-line-length = 1\n',
    )

    loaded = load_config(project)

    assert loaded.policy.header_max_length == 4
    assert loaded.policy.body_max_line_length == 1


@pytest.mark.parametrize("minimum", [0, 100_000])
def test_body_min_words_accepts_its_closed_integer_range(tmp_path: Path, minimum: int) -> None:
    project = write_pyproject(tmp_path, f"body-min-words = {minimum}\n")

    loaded = load_config(project)

    assert loaded.policy.body_min_words == minimum


@pytest.mark.parametrize("value", ["-1", "100001", "true", '"2"', "2.0"])
def test_body_min_words_rejects_invalid_values(tmp_path: Path, value: str) -> None:
    project = write_pyproject(tmp_path, f"body-min-words = {value}\n")

    with pytest.raises(ConfigurationError, match="body-min-words must be an integer"):
        load_config(project)


@pytest.mark.parametrize(
    ("minimums", "incompatible"),
    [
        ("body-min-length = 1\n", "body-min-length"),
        ("body-min-words = 1\n", "body-min-words"),
        (
            "body-min-length = 1\nbody-min-words = 1\n",
            "body-min-length and body-min-words",
        ),
    ],
)
def test_forbidden_body_requires_zero_body_minima(
    tmp_path: Path,
    minimums: str,
    incompatible: str,
) -> None:
    project = write_pyproject(
        tmp_path,
        f'body-policy = "forbidden"\n{minimums}',
    )

    with pytest.raises(
        ConfigurationError,
        match=rf"{incompatible} must be zero when body-policy is forbidden",
    ):
        load_config(project)


def test_invalid_utf8_and_oversized_files_are_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="valid UTF-8"):
        load_config(invalid)

    oversized = tmp_path / "oversized.toml"
    oversized.write_bytes(b"x" * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ConfigurationError, match="exceeds"):
        load_config(oversized)
