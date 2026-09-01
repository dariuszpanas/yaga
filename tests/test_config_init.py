"""Tests for standalone YAGA configuration initialization."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from yaga.commits import config_init
from yaga.commits.config_init import initialize_config
from yaga.commits.models import (
    BreakingMarkerPolicy,
    CasePolicy,
    EndingPolicy,
    MergePolicy,
    PresencePolicy,
)
from yaga.errors import ConfigurationError

EXPECTED_CONFIG = """config-version = 1

[commit]
allowed-types = [
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "revert",
    "style",
    "test",
]
type-case = "lower"
scope-policy = "optional"
scope-case = "lower"
header-max-length = 100
description-min-length = 3
description-ending = "forbid"
body-policy = "optional"
body-max-line-length = 100
merge-commits = "reject"
ignored-headers = []
max-commits = 64
"""


def repository(path: Path) -> Path:
    path.mkdir()
    (path / ".git").mkdir()
    return path


def test_initialize_config_writes_exact_recommended_configuration(tmp_path: Path) -> None:
    target_repository = repository(tmp_path / "repository")

    loaded = initialize_config(target_repository)

    target = target_repository / ".yaga.toml"
    assert target.read_bytes() == EXPECTED_CONFIG.encode("utf-8")
    assert loaded.path == target.resolve()
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_initialized_configuration_round_trips_to_the_recommended_policy(
    tmp_path: Path,
) -> None:
    loaded = initialize_config(repository(tmp_path / "repository"))
    policy = loaded.policy

    assert policy.config_version == 1
    assert policy.allowed_types == (
        "build",
        "chore",
        "ci",
        "docs",
        "feat",
        "fix",
        "perf",
        "refactor",
        "revert",
        "style",
        "test",
    )
    assert policy.type_case is CasePolicy.LOWER
    assert policy.scope_policy is PresencePolicy.OPTIONAL
    assert policy.allowed_scopes is None
    assert policy.scope_case is CasePolicy.LOWER
    assert policy.header_max_length == 100
    assert policy.description_min_length == 3
    assert policy.description_max_length is None
    assert policy.description_ending is EndingPolicy.FORBID
    assert policy.breaking_markers is BreakingMarkerPolicy.EITHER
    assert policy.body_policy is PresencePolicy.OPTIONAL
    assert policy.body_min_length == 0
    assert policy.body_min_words == 0
    assert policy.body_max_line_length == 100
    assert policy.merge_commits is MergePolicy.REJECT
    assert policy.ignored_headers == ()
    assert policy.max_commits == 64


def test_plain_pyproject_is_allowed_and_left_untouched(tmp_path: Path) -> None:
    target_repository = repository(tmp_path / "repository")
    pyproject = target_repository / "pyproject.toml"
    original = b'[project]\nname = "consumer"\n'
    pyproject.write_bytes(original)

    loaded = initialize_config(target_repository)

    assert loaded.path == (target_repository / ".yaga.toml").resolve()
    assert pyproject.read_bytes() == original


@pytest.mark.parametrize("filename", [".yaga.toml", "pyproject.toml"])
def test_existing_configuration_is_refused_without_modification(
    tmp_path: Path,
    filename: str,
) -> None:
    target_repository = repository(tmp_path / "repository")
    existing = target_repository / filename
    if filename == "pyproject.toml":
        content = b"[tool.yaga]\nconfig-version = 1\n"
    else:
        content = b"config-version = 1\n"
    existing.write_bytes(content)

    with pytest.raises(ConfigurationError, match="configuration already applies"):
        initialize_config(target_repository)

    assert existing.read_bytes() == content
    if filename == "pyproject.toml":
        assert not (target_repository / ".yaga.toml").exists()


def test_inherited_configuration_is_refused(tmp_path: Path) -> None:
    repository_root = repository(tmp_path / "repository")
    inherited = repository_root / ".yaga.toml"
    inherited.write_text("config-version = 1\n", encoding="utf-8")
    nested = repository_root / "nested"
    nested.mkdir()

    with pytest.raises(ConfigurationError, match="configuration already applies"):
        initialize_config(nested)

    assert not (nested / ".yaga.toml").exists()


def test_existing_directory_at_configuration_target_is_refused(tmp_path: Path) -> None:
    target_repository = repository(tmp_path / "repository")
    target = target_repository / ".yaga.toml"
    target.mkdir()

    with pytest.raises(ConfigurationError, match="configuration target already exists"):
        initialize_config(target_repository)

    assert target.is_dir()


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_repository_must_be_an_existing_directory(tmp_path: Path, kind: str) -> None:
    target = tmp_path / kind
    if kind == "file":
        target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="does not exist or is not a directory"):
        initialize_config(target)


def test_atomic_publication_never_overwrites_a_racing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repository = repository(tmp_path / "repository")
    target = target_repository / ".yaga.toml"
    original = b"racing writer\n"

    def create_racing_target(_temporary: Path, destination: Path) -> None:
        assert destination == target
        target.write_bytes(original)
        raise FileExistsError("racing target")

    publish = "rename" if os.name == "nt" else "link"
    monkeypatch.setattr(config_init.os, publish, create_racing_target)

    with pytest.raises(ConfigurationError, match="configuration target already exists"):
        config_init.initialize_config(target_repository)

    assert target.read_bytes() == original
    assert list(target_repository.glob(".yaga.toml.*.tmp")) == []


def test_temporary_file_creation_failure_is_wrapped_as_configuration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repository = repository(tmp_path / "repository")

    def deny_write(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PermissionError("write denied")

    monkeypatch.setattr(config_init.tempfile, "mkstemp", deny_write)

    with pytest.raises(ConfigurationError, match="cannot prepare configuration"):
        initialize_config(target_repository)


def test_partial_write_is_removed_so_initialization_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repository = repository(tmp_path / "repository")
    target = target_repository / ".yaga.toml"
    real_fsync = config_init.os.fsync

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_init.os, "fsync", fail_fsync)

    with pytest.raises(ConfigurationError, match="cannot prepare configuration"):
        initialize_config(target_repository)

    assert not target.exists()
    assert list(target_repository.glob(".yaga.toml.*.tmp")) == []
    monkeypatch.setattr(config_init.os, "fsync", real_fsync)
    assert initialize_config(target_repository).path == target.resolve()


def test_interrupted_write_never_publishes_a_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repository = repository(tmp_path / "repository")
    target = target_repository / ".yaga.toml"

    def interrupt_fsync(_descriptor: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(config_init.os, "fsync", interrupt_fsync)

    with pytest.raises(KeyboardInterrupt):
        initialize_config(target_repository)

    assert not target.exists()
    assert list(target_repository.glob(".yaga.toml.*.tmp")) == []
