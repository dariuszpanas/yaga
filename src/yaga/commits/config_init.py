"""Dependency-light initialization for a standalone YAGA configuration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from yaga.commits.config import load_config
from yaga.commits.models import LoadedConfig
from yaga.errors import ConfigurationError

_CONFIG_FILENAME = ".yaga.toml"
_RECOMMENDED_CONFIG = """config-version = 1

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
body-paragraph-splitting = "check"
dependabot-pull-requests = "check"
merge-commits = "reject"
ignored-headers = []
max-commits = 64
"""
_RECOMMENDED_CONFIG_BYTES = _RECOMMENDED_CONFIG.encode("utf-8")


def initialize_config(repository: Path, *, dry_run: bool = False) -> LoadedConfig:
    """Create or preview one recommended standalone configuration without overwriting."""
    try:
        resolved_repository = repository.expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            f"cannot resolve configuration repository directory {repository}: {error}"
        ) from error
    try:
        repository_is_directory = resolved_repository.is_dir()
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect configuration repository directory {resolved_repository}: {error}"
        ) from error
    if not repository_is_directory:
        raise ConfigurationError(
            f"configuration repository directory does not exist or is not a directory: "
            f"{resolved_repository}"
        )

    loaded = load_config(start=resolved_repository)
    if loaded.path is not None:
        raise ConfigurationError(
            f"configuration already applies to {resolved_repository}: {loaded.path}"
        )

    target = resolved_repository / _CONFIG_FILENAME
    _require_absent_target(target)

    try:
        temporary = _write_complete_temporary_config(resolved_repository)
    except OSError as error:
        raise ConfigurationError(f"cannot prepare configuration {target}: {error}") from error

    try:
        loaded = load_config(temporary, start=resolved_repository)
        concurrently_loaded = load_config(start=resolved_repository)
        if concurrently_loaded.path is not None:
            raise ConfigurationError(
                f"configuration already applies to {resolved_repository}: "
                f"{concurrently_loaded.path}"
            )
        if dry_run:
            return LoadedConfig(policy=loaded.policy, path=target)
        _publish_without_replacement(temporary, target)
    finally:
        _remove_temporary_config(temporary)

    return load_config(target, start=resolved_repository)


def _require_absent_target(target: Path) -> None:
    """Fail before work when any file-system entry occupies the public target."""
    try:
        target_exists = target.exists() or target.is_symlink()
    except OSError as error:
        raise ConfigurationError(
            f"cannot inspect configuration target {target}: {error}"
        ) from error
    if target_exists:
        raise ConfigurationError(f"configuration target already exists: {target}")


def _write_complete_temporary_config(repository: Path) -> Path:
    """Durably write the complete template to a private same-directory file."""
    descriptor, name = tempfile.mkstemp(
        prefix=f"{_CONFIG_FILENAME}.",
        suffix=".tmp",
        dir=repository,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            written = stream.write(_RECOMMENDED_CONFIG_BYTES)
            if written != len(_RECOMMENDED_CONFIG_BYTES):
                raise OSError("configuration write was incomplete")
            stream.flush()
            os.chmod(temporary, 0o644)
            os.fsync(stream.fileno())
    except BaseException:
        _remove_temporary_config(temporary)
        raise
    return temporary


def _publish_without_replacement(temporary: Path, target: Path) -> None:
    """Atomically publish a complete file while preserving any racing target."""
    try:
        if os.name == "nt":
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
    except OSError as error:
        try:
            _require_absent_target(target)
        except ConfigurationError as target_error:
            raise target_error from error
        raise ConfigurationError(f"cannot publish configuration {target}: {error}") from error


def _remove_temporary_config(temporary: Path) -> None:
    """Best-effort cleanup for the private randomized staging path."""
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Failure here cannot make the public target partial or replace an existing path.
        pass
