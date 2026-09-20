"""Dependency-light initialization for a standalone YAGA configuration."""

from __future__ import annotations

import os
import tempfile
from enum import StrEnum
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


class ConfigStarter(StrEnum):
    """Frozen, editable starter choices; existing defaults remain unchanged."""

    RECOMMENDED_V1 = "recommended-v1"
    TITLE_V1 = "title-v1"
    COMPLETE_MESSAGE_V1 = "complete-message-v1"
    PLAIN_V1 = "plain-v1"


_TITLE_CONFIG = """# Editable title-focused policy; YAGA still checks selected commits.
config-version = 1

[commit]
type-case = "lower"
header-max-length = 100
description-ending = "allow"
body-policy = "optional"
pull-request-message = "title-only"
merge-commits = "ignore"
"""
_COMPLETE_CONFIG = """# Explain feature and fix changes in full commit messages.
# Enable pull-request-message = "title-and-body" separately if your merge workflow uses it.
config-version = 1

[commit]
type-case = "lower"
header-max-length = 100
description-ending = "allow"
body-policy = "optional"
body-policy-by-type = {feat = "required", fix = "required"}
pull-request-message = "title-only"
merge-commits = "ignore"
"""
_PLAIN_CONFIG = """# Ordinary message structure with explicit editable limits.
config-version = 1

[commit]
message-format = "plain"
header-max-length = 100
description-ending = "allow"
body-policy = "optional"
merge-commits = "ignore"
"""
_STARTERS = {
    ConfigStarter.PLAIN_V1: _PLAIN_CONFIG.encode("utf-8"),
    ConfigStarter.RECOMMENDED_V1: _RECOMMENDED_CONFIG_BYTES,
    ConfigStarter.TITLE_V1: _TITLE_CONFIG.encode("utf-8"),
    ConfigStarter.COMPLETE_MESSAGE_V1: _COMPLETE_CONFIG.encode("utf-8"),
}


def initialize_config(
    repository: Path,
    *,
    dry_run: bool = False,
    starter: ConfigStarter = ConfigStarter.RECOMMENDED_V1,
) -> LoadedConfig:
    """Create or preview one recommended standalone configuration without overwriting."""
    if not isinstance(starter, ConfigStarter):
        raise ConfigurationError("unknown configuration starter")
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

    loaded = load_config(start=resolved_repository, use_global=False)
    if loaded.path is not None:
        raise ConfigurationError(
            f"configuration already applies to {resolved_repository}: {loaded.path}"
        )

    target = resolved_repository / _CONFIG_FILENAME
    _require_absent_target(target)

    try:
        temporary = _write_complete_temporary_config(resolved_repository, _STARTERS[starter])
    except OSError as error:
        raise ConfigurationError(f"cannot prepare configuration {target}: {error}") from error

    try:
        loaded = load_config(temporary, start=resolved_repository)
        concurrently_loaded = load_config(start=resolved_repository, use_global=False)
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


def _write_complete_temporary_config(repository: Path, content: bytes) -> Path:
    """Durably write the complete template to a private same-directory file."""
    descriptor, name = tempfile.mkstemp(
        prefix=f"{_CONFIG_FILENAME}.",
        suffix=".tmp",
        dir=repository,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            written = stream.write(content)
            if written != len(content):
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
