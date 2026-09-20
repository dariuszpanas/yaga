"""Dependency-light commit validation services shared by installed commands."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from yaga.commits.checker import check_header, check_target
from yaga.commits.config import load_config
from yaga.commits.git import read_commit, read_range
from yaga.commits.models import (
    CheckResult,
    CommitPolicy,
    CommitTarget,
    QualityPolicy,
    QualityProvider,
    ValidationReport,
)
from yaga.commits.quality import (
    QualityReport,
    check_quality,
)
from yaga.commits.sources import from_edit, from_file, from_message, from_stdin, validate_title
from yaga.commits.typos import apply_typos
from yaga.errors import InputError


def check_commits(
    repository: Path,
    *,
    message: str | None = None,
    title: str | None = None,
    edit: Path | None = None,
    file: Path | None = None,
    stdin: bool = False,
    commit: str | None = None,
    revision_range: str | None = None,
    config: Path | None = None,
) -> ValidationReport:
    """Check HEAD or exactly one explicitly selected commit-message source."""
    selected = [
        title is not None,
        edit is not None,
        message is not None,
        file is not None,
        stdin,
        commit is not None,
        revision_range is not None,
    ]
    if sum(selected) > 1:
        raise InputError(
            "choose only one of --title, --edit, --message, --file, --stdin, --commit, or --range"
        )

    repo = _resolve_repository(repository)
    loaded = load_config(config, start=repo)
    if title is not None:
        target = CommitTarget("title", validate_title(title))
        result = apply_typos(check_header(target, loaded.policy), loaded.policy, repository=repo)
        return ValidationReport(
            results=(result,),
            config_path=loaded.path,
            report_version=2 if loaded.policy.warning_rules else 1,
        )
    if edit is not None:
        return _check_targets(
            [from_edit(edit, repository=repo)],
            config_path=loaded.path,
            policy=loaded.policy,
            repository=repo,
        )
    targets = _select_targets(
        message=message,
        file=file,
        stdin=stdin,
        commit=commit,
        revision_range=revision_range,
        repository=repo,
        max_commits=loaded.policy.max_commits,
    )
    return _check_targets(
        targets,
        config_path=loaded.path,
        policy=loaded.policy,
        repository=repo,
    )


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
    return _check_targets(
        targets,
        config_path=loaded.path,
        policy=loaded.policy,
        repository=repo,
    )


def check_commit_quality(
    repository: Path,
    *,
    config: Path | None = None,
    message: str | None = None,
    file: Path | None = None,
    stdin: bool = False,
    commit: str | None = None,
    revision_range: str | None = None,
    provider: str | None = None,
    task: str | None = None,
    model_id: str | None = None,
    revision: str | None = None,
    threshold: float | None = None,
    offline: bool = False,
    region: str | None = None,
    max_tokens: int | None = None,
    max_input_tokens: int | None = None,
    input_mode: str | None = None,
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
    settings = load_config(config, start=repo).policy.quality
    if provider is not None and provider != settings.provider.value:
        try:
            provider_defaults = QualityPolicy.for_provider(QualityProvider(provider))
        except ValueError as error:
            raise InputError("quality provider must be one of: bedrock, huggingface") from error
        settings = replace(
            settings,
            provider=provider_defaults.provider,
            task=provider_defaults.task,
            model_id=provider_defaults.model_id,
            revision=provider_defaults.revision,
            region=provider_defaults.region,
        )
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
        provider=settings.provider.value,
        task=task if task is not None else settings.task.value,
        model_id=model_id if model_id is not None else settings.model_id,
        revision=revision if revision is not None else settings.revision,
        threshold=threshold if threshold is not None else settings.threshold,
        offline=offline,
        region=region if region is not None else settings.region,
        max_tokens=max_tokens if max_tokens is not None else settings.max_tokens,
        max_input_tokens=(
            max_input_tokens if max_input_tokens is not None else settings.max_input_tokens
        ),
        input_mode=input_mode if input_mode is not None else settings.input_mode.value,
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
    repository: Path,
) -> ValidationReport:
    results = tuple(_check_target(target, policy, repository=repository) for target in targets)
    return ValidationReport(
        results=results, config_path=config_path, report_version=2 if policy.warning_rules else 1
    )


def _check_target(target: CommitTarget, policy: CommitPolicy, *, repository: Path) -> CheckResult:
    return apply_typos(check_target(target, policy), policy, repository=repository)
