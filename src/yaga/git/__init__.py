"""Shared bounded Git repository and process primitives."""

from yaga.git.runtime import (
    GitRepository,
    ProcessResult,
    open_repository,
    parse_object_id,
    require_complete_history,
    require_no_legacy_grafts,
    resolve_common_git_directory,
    run_git,
    safe_git_error,
)
from yaga.git.tree import CommittedTreeIdentity, resolve_committed_tree

__all__ = (
    "GitRepository",
    "ProcessResult",
    "CommittedTreeIdentity",
    "open_repository",
    "parse_object_id",
    "require_complete_history",
    "require_no_legacy_grafts",
    "resolve_committed_tree",
    "resolve_common_git_directory",
    "run_git",
    "safe_git_error",
)
