"""Closed dependency-free command boundary for the commit-check Action."""

from __future__ import annotations

import os
import re
import sys

from yaga.commit_action_runtime import run_pull_request_action
from yaga.errors import InputError, safe_error_text

_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]{0,18}\Z")
_MAX_REPOSITORY_ID = 9_223_372_036_854_775_807


def main(argv: list[str] | None = None) -> int:
    """Accept the one command wired by the read-only composite Action."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if (
        len(arguments) != 9
        or arguments[:3] != ["github", "pull-request", "check"]
        or arguments[3] != "--event-file"
        or arguments[5] != "--repo"
        or arguments[7:] != ["--format", "github"]
    ):
        print("YAGA failed: commit action command is invalid", file=sys.stderr)
        return 2
    try:
        repository_id = _repository_id(_required_environment("GITHUB_REPOSITORY_ID"))
        event_name = _required_environment("GITHUB_EVENT_NAME")
        repository = _required_environment("GITHUB_REPOSITORY")
        base_ref = _required_environment("GITHUB_BASE_REF")
        head_ref = _required_environment("GITHUB_HEAD_REF")
        trusted_config = os.environ.get("YAGA_COMMIT_TRUSTED_CONFIG") or None
        trusted_revision = _required_environment("GITHUB_SHA") if trusted_config else None
        trusted_ref = _required_environment("GITHUB_REF") if trusted_config else None
    except InputError as error:
        print(f"YAGA failed: {safe_error_text(error)}", file=sys.stderr)
        return 2
    return run_pull_request_action(
        arguments[4],
        arguments[6],
        event_name=event_name,
        repository=repository,
        repository_id=repository_id,
        base_ref=base_ref,
        head_ref=head_ref,
        trusted_config=trusted_config,
        trusted_revision=trusted_revision,
        trusted_ref=trusted_ref,
    )


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise InputError(f"{name} is required for the commit Action")


def _repository_id(value: str) -> int:
    if _POSITIVE_INTEGER.fullmatch(value) is None:
        raise InputError("GitHub repository ID is invalid")
    parsed = int(value)
    if parsed > _MAX_REPOSITORY_ID:
        raise InputError("GitHub repository ID is invalid")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
