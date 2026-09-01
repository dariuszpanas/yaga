"""Shared bounded Git repository and process primitives."""

from yaga.git.runtime import (
    GitRepository,
    ProcessResult,
    open_repository,
    parse_object_id,
    require_complete_history,
    resolve_common_git_directory,
    run_git,
    safe_git_error,
)

__all__ = (
    "GitRepository",
    "ProcessResult",
    "open_repository",
    "parse_object_id",
    "require_complete_history",
    "resolve_common_git_directory",
    "run_git",
    "safe_git_error",
)
