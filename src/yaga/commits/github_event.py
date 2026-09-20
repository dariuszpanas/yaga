"""Strict GitHub pull-request selection for commit-policy checks."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yaga.commits.checker import check_header, check_target
from yaga.commits.config import load_config
from yaga.commits.git import read_commit, read_range
from yaga.commits.models import CheckResult, CommitTarget, DependabotPullRequestPolicy
from yaga.commits.parser import header_from
from yaga.commits.sources import MAX_TITLE_BYTES, validate_title
from yaga.commits.trusted_policy import load_trusted_policy
from yaga.commits.typos import apply_typos
from yaga.errors import GitError, InputError
from yaga.files import read_file_prefix

MAX_PULL_REQUEST_EVENT_BYTES = 1024 * 1024
MAX_PULL_REQUEST_TITLE_BYTES = MAX_TITLE_BYTES
MAX_PULL_REQUEST_NUMBER = 9_223_372_036_854_775_807
MAX_PULL_REQUEST_AUTHOR_LOGIN_BYTES = 128
MAX_PULL_REQUEST_AUTHOR_TYPE_BYTES = 32
MAX_REF_BYTES = 255
DEPENDABOT_PULL_REQUEST_SKIP_REASON = "Dependabot pull request"

_ACTION = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_AUTHOR_LOGIN = re.compile(r"[A-Za-z0-9_.-]+(?:\[bot\])?\Z")
_AUTHOR_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}\Z")
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
_SUPPORTED_ACTIONS = frozenset({"edited", "opened", "ready_for_review", "reopened", "synchronize"})


@dataclass(frozen=True, slots=True)
class PullRequestEvent:
    """Commit-policy fields selected from one GitHub pull-request event."""

    action: str
    number: int
    title: str
    base_sha: str
    head_sha: str
    base_ref: str
    head_ref: str
    repository: str
    repository_id: int
    draft: bool
    author_login: str
    author_id: int
    author_type: str
    head_repository: str = ""
    default_branch: str | None = None


@dataclass(frozen=True, slots=True)
class PullRequestValidationReport:
    """Complete title and commit-policy result for one pull request."""

    event: PullRequestEvent
    title: CheckResult
    commits: tuple[CheckResult, ...]
    config_path: Path | None

    @property
    def results(self) -> tuple[CheckResult, ...]:
        """Return title first, followed by commits in oldest-first order."""
        return (self.title, *self.commits)

    @property
    def failed(self) -> int:
        """Return the number of failed title or commit targets."""
        return sum(result.status == "failed" for result in self.results)

    @property
    def passed(self) -> int:
        """Return the number of passed title or commit targets."""
        return sum(result.status == "passed" for result in self.results)

    @property
    def skipped(self) -> int:
        """Return the number of skipped title or commit targets."""
        return sum(result.status == "skipped" for result in self.results)

    @property
    def valid(self) -> bool:
        """Return whether both the title and every selected commit passed."""
        return self.failed == 0


def check_pull_request(
    event_file: Path,
    repository: Path,
    *,
    config: Path | None = None,
    expected_event_name: str | None = None,
    expected_repository: str | None = None,
    expected_repository_id: int | None = None,
    expected_base_ref: str | None = None,
    expected_head_ref: str | None = None,
    trusted_config: str | None = None,
    trusted_revision: str | None = None,
    trusted_ref: str | None = None,
) -> PullRequestValidationReport:
    """Check one event title and its exact base-to-head commit selection."""
    event = load_pull_request_event(event_file)
    trusted = trusted_config is not None
    if trusted != (trusted_revision is not None) or trusted != (trusted_ref is not None):
        raise InputError("trusted policy requires a config path, revision, and runner ref together")
    if trusted and (config is not None or expected_event_name != "pull_request_target"):
        raise InputError(
            "trusted policy requires pull_request_target runner context and no --config"
        )
    repo = repository.expanduser().resolve()
    if not repo.is_dir():
        raise InputError("repository directory does not exist")
    context = (
        expected_event_name,
        expected_repository,
        expected_repository_id,
        expected_base_ref,
        expected_head_ref,
    )
    if any(value is not None for value in context):
        if any(value is None for value in context):
            raise InputError("GitHub Action context is incomplete")
        assert expected_event_name is not None
        assert expected_repository is not None
        assert expected_repository_id is not None
        assert expected_base_ref is not None
        assert expected_head_ref is not None
        validate_action_context(
            event,
            event_name=expected_event_name,
            trusted_policy=trusted,
            repository=expected_repository,
            repository_id=expected_repository_id,
            base_ref=expected_base_ref,
            head_ref=expected_head_ref,
        )
    checkout = read_commit(repo, "HEAD")
    if not trusted and checkout.sha != event.head_sha:
        raise GitError("repository HEAD does not match the pull request head SHA")
    if trusted:
        if not event.default_branch or trusted_ref != f"refs/heads/{event.default_branch}":
            raise InputError("trusted policy requires the default-branch runner ref")
        if checkout.sha != trusted_revision:
            raise GitError("trusted checkout HEAD does not match the runner SHA")
        fetched = read_commit(repo, f"refs/remotes/pull/{event.number}/head")
        if fetched.sha != event.head_sha:
            raise GitError("fetched pull request head does not match the event head SHA")
        assert trusted_config is not None and trusted_revision is not None
        loaded = load_trusted_policy(repo, trusted_revision, trusted_config)
    else:
        loaded = load_config(config, start=repo)
    targets = read_range(
        repo,
        f"{event.base_sha}..{event.head_sha}",
        max_commits=loaded.policy.max_commits,
    )
    title_target = CommitTarget(
        label=f"pull request #{event.number} title",
        message=event.title,
    )
    dependabot_skip = (
        loaded.policy.dependabot_pull_requests is DependabotPullRequestPolicy.SKIP
        and event.author_login == "dependabot[bot]"
        and event.author_type == "Bot"
        and (
            not trusted
            or (
                event.head_repository == event.repository
                and event.head_ref.startswith("dependabot/")
            )
        )
    )
    configured_skip = event.author_login.casefold() in {
        login.casefold() for login in loaded.policy.skip_pull_request_authors
    }
    if dependabot_skip or configured_skip:
        reason = (
            DEPENDABOT_PULL_REQUEST_SKIP_REASON
            if dependabot_skip
            else "Configured pull request author"
        )
        title = _skip_result(title_target, reason)
        commits = tuple(_skip_result(target, reason) for target in targets)
    else:
        title = apply_typos(
            check_header(title_target, loaded.policy),
            loaded.policy,
            repository=repo,
            isolated=trusted,
        )
        commits = tuple(
            apply_typos(
                check_target(target, loaded.policy),
                loaded.policy,
                repository=repo,
                isolated=trusted,
            )
            for target in targets
        )
    return PullRequestValidationReport(
        event=event,
        title=title,
        commits=commits,
        config_path=loaded.path,
    )


def load_pull_request_event(path: Path) -> PullRequestEvent:
    """Strictly parse one bounded GitHub pull-request event file."""
    resolved = path.expanduser().resolve()
    try:
        raw = read_file_prefix(resolved, maximum=MAX_PULL_REQUEST_EVENT_BYTES)
    except OSError as error:
        raise InputError("pull request event could not be read") from error
    if not raw:
        raise InputError("pull request event is empty")
    if len(raw) > MAX_PULL_REQUEST_EVENT_BYTES:
        raise InputError(
            f"pull request event exceeds the hard {MAX_PULL_REQUEST_EVENT_BYTES}-byte limit"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError("pull request event is not valid UTF-8") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise InputError("pull request event is invalid or ambiguous JSON") from error

    root = _object(document, "pull request event")
    pull_request = _object(root.get("pull_request"), "pull request")
    author = _object(pull_request.get("user"), "pull request author")
    author_login = _author_login(author.get("login"))
    author_id = _positive_int(author.get("id"), "pull request author ID")
    author_type = _author_type(author.get("type"))
    repository = _object(root.get("repository"), "event repository")
    repository_id = _positive_int(repository.get("id"), "event repository ID")
    repository_name = _repository_name(repository.get("full_name"), "event repository")
    number = _positive_int(root.get("number"), "pull request number")
    nested_number = _positive_int(pull_request.get("number"), "pull request object number")
    if number != nested_number:
        raise InputError("pull request numbers do not match")

    action = root.get("action")
    if (
        not isinstance(action, str)
        or _ACTION.fullmatch(action) is None
        or action not in _SUPPORTED_ACTIONS
    ):
        raise InputError("pull request action is invalid")
    if pull_request.get("state") != "open":
        raise InputError("pull request state must be open")
    draft = pull_request.get("draft")
    if not isinstance(draft, bool):
        raise InputError("pull request draft state must be a boolean")
    if action == "ready_for_review" and draft:
        raise InputError("ready_for_review event cannot describe a draft pull request")
    title = _title(pull_request.get("title"))
    base = _object(pull_request.get("base"), "pull request base")
    head = _object(pull_request.get("head"), "pull request head")
    base_repository = _object(base.get("repo"), "pull request base repository")
    base_repository_id = _positive_int(base_repository.get("id"), "pull request base repository ID")
    base_repository_name = _repository_name(
        base_repository.get("full_name"), "pull request base repository"
    )
    if base_repository_id != repository_id or base_repository_name != repository_name:
        raise InputError("pull request base repository does not match the event repository")
    head_repository = _object(head.get("repo"), "pull request head repository")
    head_repository_id = _positive_int(head_repository.get("id"), "pull request head repository ID")
    head_repository_name = _repository_name(
        head_repository.get("full_name"), "pull request head repository"
    )
    if (head_repository_id == repository_id) != (head_repository_name == repository_name):
        raise InputError("pull request head repository identity is inconsistent")
    base_sha = _object_id(base.get("sha"), "pull request base SHA")
    head_sha = _object_id(head.get("sha"), "pull request head SHA")
    base_ref = _ref(base.get("ref"), "pull request base ref")
    head_ref = _ref(head.get("ref"), "pull request head ref")
    if len(base_sha) != len(head_sha):
        raise InputError("pull request base and head use different object ID formats")
    if base_sha == head_sha:
        raise InputError("pull request base and head must differ")
    return PullRequestEvent(
        action=action,
        number=number,
        title=title,
        base_sha=base_sha,
        head_sha=head_sha,
        base_ref=base_ref,
        head_ref=head_ref,
        repository=repository_name,
        repository_id=repository_id,
        draft=draft,
        author_login=author_login,
        author_id=author_id,
        author_type=author_type,
        head_repository=head_repository_name,
        default_branch=(
            _ref(repository["default_branch"], "repository default branch")
            if "default_branch" in repository
            else None
        ),
    )


def validate_action_context(
    event: PullRequestEvent,
    *,
    event_name: str,
    repository: str,
    repository_id: int,
    base_ref: str,
    head_ref: str,
    trusted_policy: bool = False,
) -> None:
    """Bind a parsed payload to the immutable GitHub runner context."""
    required_event = "pull_request_target" if trusted_policy else "pull_request"
    if event_name != required_event:
        raise InputError(f"GitHub event name must be {required_event}")
    if repository != event.repository:
        raise InputError("GitHub repository does not match the event payload")
    if (
        isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or repository_id != event.repository_id
    ):
        raise InputError("GitHub repository ID does not match the event payload")
    if base_ref != event.base_ref:
        raise InputError("GitHub base ref does not match the event payload")
    if head_ref != event.head_ref:
        raise InputError("GitHub head ref does not match the event payload")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputError(f"{label} must be an object")
    return value


def _positive_int(value: object, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PULL_REQUEST_NUMBER
    ):
        raise InputError(f"{label} must be a bounded positive integer")
    return value


def _author_login(value: object) -> str:
    if not isinstance(value, str):
        raise InputError("pull request author login must be a bounded GitHub login")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise InputError("pull request author login must be a bounded GitHub login") from error
    if (
        not encoded
        or len(encoded) > MAX_PULL_REQUEST_AUTHOR_LOGIN_BYTES
        or _AUTHOR_LOGIN.fullmatch(value) is None
    ):
        raise InputError("pull request author login must be a bounded GitHub login")
    return value


def _author_type(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_PULL_REQUEST_AUTHOR_TYPE_BYTES
        or _AUTHOR_TYPE.fullmatch(value) is None
    ):
        raise InputError("pull request author type must be a bounded GitHub account type")
    return value


def _skip_result(target: CommitTarget, reason: str) -> CheckResult:
    return CheckResult(
        target=target,
        header=header_from(target.message),
        skipped_reason=reason,
    )


def _object_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise InputError(f"{label} must be a full lowercase object ID")
    return value


def _repository_name(value: object, label: str) -> str:
    if not isinstance(value, str) or _REPOSITORY.fullmatch(value) is None:
        raise InputError(f"{label} must be an owner/name pair")
    if any(component in {".", ".."} for component in value.split("/")):
        raise InputError(f"{label} contains an unsafe path segment")
    return value


def _ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise InputError(f"{label} must be a bounded ref name")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError(f"{label} must be valid UTF-8") from error
    if len(encoded) > MAX_REF_BYTES or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for character in value
    ):
        raise InputError(f"{label} contains unsafe or oversized text")
    return value


def _title(value: object) -> str:
    return validate_title(value, label="pull request title")
