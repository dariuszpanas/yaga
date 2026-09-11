"""Dependency-light commit validation services shared by installed commands."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from yaga.commits.checker import check_target
from yaga.commits.config import load_config
from yaga.commits.git import read_commit, read_range
from yaga.commits.models import (
    CheckResult,
    CommitPolicy,
    CommitTarget,
    TyposPolicy,
    ValidationReport,
)
from yaga.commits.quality import (
    DEFAULT_LOW_QUALITY_THRESHOLD,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REVISION,
    QualityReport,
    check_quality,
)
from yaga.commits.sources import from_file, from_message, from_stdin
from yaga.commits.typos import check_typos
from yaga.errors import InputError


def check_commits(
    repository: Path,
    *,
    message: str | None = None,
    file: Path | None = None,
    stdin: bool = False,
    commit: str | None = None,
    revision_range: str | None = None,
    config: Path | None = None,
) -> ValidationReport:
    """Check HEAD or exactly one explicitly selected commit-message source."""
    selected = [
        message is not None,
        file is not None,
        stdin,
        commit is not None,
        revision_range is not None,
    ]
    if sum(selected) > 1:
        raise InputError("choose only one of --message, --file, --stdin, --commit, or --range")

    repo = _resolve_repository(repository)
    loaded = load_config(config, start=repo)
    targets = _select_targets(
        message=message,
        file=file,
        stdin=stdin,
        commit=commit,
        revision_range=revision_range,
        repository=repo,
        max_commits=loaded.policy.max_commits,
    )
    return _check_targets(targets, config_path=loaded.path, policy=loaded.policy)


def check_git_commits(
    repository: Path,
    *,
    commit: str | None = None,
    revision_range: str | None = None,
    config: Path | None = None,
) -> ValidationReport:
    """Check one Git revision, one Git range, or HEAD by default."""
    if commit is not None and revision_range is not None:
        raise InputError("choose only one of --commit or --range")

    repo = _resolve_repository(repository)
    loaded = load_config(config, start=repo)
    targets = _select_git_targets(
        repository=repo,
        commit=commit,
        revision_range=revision_range,
        max_commits=loaded.policy.max_commits,
    )
    return _check_targets(targets, config_path=loaded.path, policy=loaded.policy)


def check_commit_quality(
    repository: Path,
    *,
    message: str | None = None,
    file: Path | None = None,
    stdin: bool = False,
    commit: str | None = None,
    revision_range: str | None = None,
    model_id: str = DEFAULT_MODEL_ID,
    revision: str = DEFAULT_MODEL_REVISION,
    threshold: float = DEFAULT_LOW_QUALITY_THRESHOLD,
    offline: bool = False,
) -> QualityReport:
    """Run the opt-in quality model against one explicit source selection."""
    selected = [
        message is not None,
        file is not None,
        stdin,
        commit is not None,
        revision_range is not None,
    ]
    if sum(selected) > 1:
        raise InputError("choose only one of --message, --file, --stdin, --commit, or --range")
    repo = _resolve_repository(repository)
    targets = _select_targets(
        message=message,
        file=file,
        stdin=stdin,
        commit=commit,
        revision_range=revision_range,
        repository=repo,
        max_commits=256,
    )
    return check_quality(
        targets,
        model_id=model_id,
        revision=revision,
        threshold=threshold,
        offline=offline,
    )


def _resolve_repository(repository: Path) -> Path:
    try:
        return repository.expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise InputError("commit repository path cannot be resolved") from error


def _select_targets(
    *,
    message: str | None,
    file: Path | None,
    stdin: bool,
    commit: str | None,
    revision_range: str | None,
    repository: Path,
    max_commits: int,
) -> list[CommitTarget]:
    if message is not None:
        return [from_message(message)]
    if file is not None:
        return [from_file(file)]
    if stdin:
        return [from_stdin()]
    return _select_git_targets(
        repository=repository,
        commit=commit,
        revision_range=revision_range,
        max_commits=max_commits,
    )


def _select_git_targets(
    *,
    repository: Path,
    commit: str | None,
    revision_range: str | None,
    max_commits: int,
) -> list[CommitTarget]:
    if revision_range is not None:
        return read_range(repository, revision_range, max_commits=max_commits)
    return [read_commit(repository, commit or "HEAD")]


def _check_targets(
    targets: list[CommitTarget],
    *,
    config_path: Path | None,
    policy: CommitPolicy,
) -> ValidationReport:
    results = tuple(_check_target(target, policy) for target in targets)
    return ValidationReport(results=results, config_path=config_path)


def _check_target(target: CommitTarget, policy: CommitPolicy) -> CheckResult:
    result = check_target(target, policy)
    if policy.typos is not TyposPolicy.CHECK or result.skipped_reason is not None:
        return result
    diagnostics = check_typos(target.message)
    if not diagnostics:
        return result
    return replace(result, diagnostics=(*result.diagnostics, *diagnostics))
