"""Tests for strict GitHub pull-request commit selection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from yaga.commits import github_event
from yaga.commits.github_event import (
    MAX_PULL_REQUEST_EVENT_BYTES,
    check_pull_request,
    load_pull_request_event,
    validate_action_context,
)
from yaga.errors import ConfigurationError, GitError, InputError

DEPENDABOT_AUTHOR = {"login": "dependabot[bot]", "id": 49_699_333, "type": "Bot"}


def _write_skip_config(repository: Path, *, extra: str = "") -> Path:
    config = repository / ".yaga.toml"
    config.write_text(
        'config-version = 1\n\n[commit]\ndependabot-pull-requests = "skip"\n' + extra,
        encoding="utf-8",
    )
    return config


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(
    tmp_path: Path,
    *,
    head_message: str = "feat(cli): add pull request checks",
) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    base = _commit(repository, "chore: establish baseline", "base")
    head = _commit(repository, head_message, "head")
    return repository, base, head


def _commit(repository: Path, message: str, content: str) -> str:
    (repository / "content.txt").write_text(content, encoding="utf-8")
    _git(repository, "add", "--", "content.txt")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _event_document(
    base: str,
    head: str,
    *,
    title: object = "feat(cli): check pull requests",
    author: object = None,
) -> dict[str, object]:
    if author is None:
        author = {"login": "octocat", "id": 1, "type": "User"}
    return {
        "action": "synchronize",
        "number": 17,
        "repository": {"id": 100, "full_name": "owner/repository"},
        "pull_request": {
            "number": 17,
            "title": title,
            "state": "open",
            "draft": False,
            "user": author,
            "base": {
                "sha": base,
                "ref": "main",
                "repo": {"id": 100, "full_name": "owner/repository"},
            },
            "head": {
                "sha": head,
                "ref": "feature",
                "repo": {"id": 100, "full_name": "owner/repository"},
            },
        },
    }


def _write_event(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_check_uses_the_exact_event_base_and_head(tmp_path: Path) -> None:
    repository, base, head = _repository(tmp_path)
    event_file = _write_event(tmp_path / "event.json", _event_document(base, head))

    report = check_pull_request(event_file, repository)

    assert report.valid
    assert report.event.number == 17
    assert report.title.target.label == "pull request #17 title"
    assert [result.target.sha for result in report.commits] == [head]
    assert report.commits[0].header == "feat(cli): add pull request checks"


def test_dependabot_pull_requests_are_checked_by_default(tmp_path: Path) -> None:
    repository, base, head = _repository(tmp_path, head_message="not conventional")
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="also not conventional", author=DEPENDABOT_AUTHOR),
    )

    report = check_pull_request(event_file, repository)

    assert not report.valid
    assert report.failed == 2
    assert report.skipped == 0
    assert report.event.author_login == "dependabot[bot]"
    assert report.event.author_id == 49_699_333
    assert report.event.author_type == "Bot"
    assert all(result.skipped_reason is None for result in report.results)


@pytest.mark.parametrize(
    "action",
    ["opened", "edited", "reopened", "ready_for_review", "synchronize"],
)
@pytest.mark.parametrize("author_id", [1, 9_223_372_036_854_775_807])
def test_explicit_policy_skips_only_exact_dependabot_pull_request_identity(
    tmp_path: Path,
    action: str,
    author_id: int,
) -> None:
    repository, base, head = _repository(tmp_path, head_message="not conventional")
    config = _write_skip_config(repository)
    event_document = _event_document(
        base,
        head,
        title="also not conventional",
        author={"login": "dependabot[bot]", "id": author_id, "type": "Bot"},
    )
    event_document["action"] = action
    event_file = _write_event(
        tmp_path / "event.json",
        event_document,
    )

    report = check_pull_request(event_file, repository)

    assert report.valid
    assert report.failed == 0
    assert report.skipped == 2
    assert report.config_path == config
    assert [result.skipped_reason for result in report.results] == [
        "Dependabot pull request",
        "Dependabot pull request",
    ]


@pytest.mark.parametrize(
    "author",
    [
        {"login": "octocat", "id": 1, "type": "User"},
        {"login": "dependabot", "id": 49_699_333, "type": "Bot"},
        {"login": "Dependabot[bot]", "id": 49_699_333, "type": "Bot"},
        {"login": "dependabot[bot]", "id": 49_699_333, "type": "User"},
    ],
)
def test_dependabot_skip_does_not_admit_well_formed_identity_near_misses(
    tmp_path: Path,
    author: dict[str, object],
) -> None:
    repository, base, head = _repository(tmp_path, head_message="not conventional")
    _write_skip_config(repository)
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="also not conventional", author=author),
    )

    report = check_pull_request(event_file, repository)

    assert not report.valid
    assert report.failed == 2
    assert report.skipped == 0


def test_check_rejects_a_checkout_that_is_not_the_event_head(tmp_path: Path) -> None:
    repository, base, event_head = _repository(tmp_path)
    _commit(repository, "fix: move checkout", "later checkout state")
    _write_skip_config(repository)
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, event_head, author=DEPENDABOT_AUTHOR),
    )

    with pytest.raises(GitError, match="HEAD does not match"):
        check_pull_request(event_file, repository)


def test_dependabot_skip_does_not_hide_missing_history(tmp_path: Path) -> None:
    repository, _base, head = _repository(tmp_path, head_message="not conventional")
    _write_skip_config(repository)
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(
            "0" * 40,
            head,
            title="also not conventional",
            author=DEPENDABOT_AUTHOR,
        ),
    )

    with pytest.raises(GitError):
        check_pull_request(event_file, repository)


def test_dependabot_skip_does_not_hide_commit_count_overflow(tmp_path: Path) -> None:
    repository, base, _middle = _repository(tmp_path, head_message="not conventional")
    head = _commit(repository, "still not conventional", "second invalid commit")
    _write_skip_config(repository, extra="max-commits = 1\n")
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(
            base,
            head,
            title="also not conventional",
            author=DEPENDABOT_AUTHOR,
        ),
    )

    with pytest.raises(GitError, match="exceeds the configured limit of 1"):
        check_pull_request(event_file, repository)


def test_dependabot_skip_does_not_hide_configuration_errors(tmp_path: Path) -> None:
    repository, base, head = _repository(tmp_path, head_message="not conventional")
    _write_skip_config(repository, extra="unknown = true\n")
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(
            base,
            head,
            title="also not conventional",
            author=DEPENDABOT_AUTHOR,
        ),
    )

    with pytest.raises(ConfigurationError, match="unknown commit configuration key"):
        check_pull_request(event_file, repository)


def test_title_and_commits_share_the_discovered_policy(tmp_path: Path) -> None:
    repository, base, head = _repository(tmp_path)
    (repository / ".yaga.toml").write_text(
        """\
config-version = 1

[commit]
allowed-types = ["fix"]
body-policy = "required"
""",
        encoding="utf-8",
    )
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document(base, head, title="fix: prepare pull request"),
    )

    report = check_pull_request(event_file, repository)

    assert not report.valid
    assert report.title.valid
    assert [diagnostic.code for diagnostic in report.commits[0].diagnostics] == [
        "type.allowed",
        "body.required",
    ]
    assert report.config_path == repository / ".yaga.toml"


def test_event_parser_requires_matching_numbers_and_safe_title(tmp_path: Path) -> None:
    sha = "a" * 40
    document = _event_document(sha, "b" * 40)
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["number"] = 18
    event_file = _write_event(tmp_path / "event.json", document)

    with pytest.raises(InputError, match="numbers do not match"):
        load_pull_request_event(event_file)

    pull_request["number"] = 17
    pull_request["title"] = "feat: unsafe\nsecond line"
    _write_event(event_file, document)
    with pytest.raises(InputError, match="unsafe characters"):
        load_pull_request_event(event_file)


def test_event_parser_requires_one_complete_author_object(tmp_path: Path) -> None:
    document = _event_document("a" * 40, "b" * 40)
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request.pop("user")
    event_file = _write_event(tmp_path / "event.json", document)

    with pytest.raises(InputError, match="pull request author must be an object"):
        load_pull_request_event(event_file)

    pull_request["user"] = "dependabot[bot]"
    _write_event(event_file, document)
    with pytest.raises(InputError, match="pull request author must be an object"):
        load_pull_request_event(event_file)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("login", None, "author login"),
        ("login", "", "author login"),
        ("login", "dépendabot[bot]", "author login"),
        ("login", "x" * 129, "author login"),
        ("id", None, "author ID"),
        ("id", 0, "author ID"),
        ("id", True, "author ID"),
        ("id", 9_223_372_036_854_775_808, "author ID"),
        ("type", None, "author type"),
        ("type", "", "author type"),
        ("type", "Bot\n", "author type"),
        ("type", "B" * 33, "author type"),
    ],
)
def test_event_parser_rejects_malformed_author_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    document = _event_document("a" * 40, "b" * 40, author=dict(DEPENDABOT_AUTHOR))
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    author = pull_request["user"]
    assert isinstance(author, dict)
    author[field] = value
    event_file = _write_event(tmp_path / "event.json", document)

    with pytest.raises(InputError, match=message):
        load_pull_request_event(event_file)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("action", "OPENED", "action is invalid"),
        ("number", True, "bounded positive integer"),
        ("title", "feat: bad\u202etitle", "unsafe characters"),
        ("title", "\ud800", "not valid UTF-8"),
        ("base_sha", "A" * 40, "full lowercase object ID"),
        ("head_sha", "b" * 39, "full lowercase object ID"),
    ],
)
def test_event_parser_rejects_malformed_boundary_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    document = _event_document("a" * 40, "b" * 40)
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    if field == "title":
        pull_request["title"] = value
    elif field == "base_sha":
        base = pull_request["base"]
        assert isinstance(base, dict)
        base["sha"] = value
    elif field == "head_sha":
        head = pull_request["head"]
        assert isinstance(head, dict)
        head["sha"] = value
    else:
        document[field] = value
    event_file = _write_event(tmp_path / "event.json", document)

    with pytest.raises(InputError, match=message):
        load_pull_request_event(event_file)


def test_event_parser_rejects_ambiguous_and_nonstandard_json(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text('{"action":"opened","action":"closed"}', encoding="utf-8")
    with pytest.raises(InputError, match="invalid or ambiguous JSON"):
        load_pull_request_event(event_file)

    event_file.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(InputError, match="invalid or ambiguous JSON"):
        load_pull_request_event(event_file)


def test_event_parser_normalizes_json_recursion_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text("{}", encoding="utf-8")

    def recurse(*_args: object, **_kwargs: object) -> object:
        raise RecursionError

    monkeypatch.setattr(github_event.json, "loads", recurse)

    with pytest.raises(InputError, match="invalid or ambiguous JSON"):
        load_pull_request_event(event_file)


def test_event_parser_preserves_policy_relevant_title_whitespace(tmp_path: Path) -> None:
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document("a" * 40, "b" * 40, title=" feat: padded"),
    )

    assert load_pull_request_event(event_file).title == " feat: padded"

    _write_event(
        event_file,
        _event_document("a" * 40, "b" * 40, title="feat: support 👩‍💻"),
    )
    assert load_pull_request_event(event_file).title == "feat: support 👩‍💻"


def test_event_parser_bounds_and_strictly_decodes_the_file(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_bytes(b"\xff")
    with pytest.raises(InputError, match="not valid UTF-8"):
        load_pull_request_event(event_file)

    event_file.write_bytes(b" " * (MAX_PULL_REQUEST_EVENT_BYTES + 1))
    with pytest.raises(InputError, match="exceeds the hard"):
        load_pull_request_event(event_file)


def test_event_parser_rejects_different_or_equal_object_id_boundaries(tmp_path: Path) -> None:
    event_file = _write_event(
        tmp_path / "event.json",
        _event_document("a" * 40, "b" * 64),
    )
    with pytest.raises(InputError, match="different object ID formats"):
        load_pull_request_event(event_file)

    _write_event(event_file, _event_document("a" * 40, "a" * 40))
    with pytest.raises(InputError, match="must differ"):
        load_pull_request_event(event_file)


@pytest.mark.parametrize(
    "action",
    ["edited", "opened", "ready_for_review", "reopened", "synchronize"],
)
def test_event_parser_accepts_only_supported_open_lifecycle_actions(
    tmp_path: Path,
    action: str,
) -> None:
    document = _event_document("a" * 40, "b" * 40)
    document["action"] = action
    event_file = _write_event(tmp_path / "event.json", document)

    assert load_pull_request_event(event_file).action == action

    document["action"] = "closed"
    _write_event(event_file, document)
    with pytest.raises(InputError, match="action is invalid"):
        load_pull_request_event(event_file)


def test_event_parser_rejects_displaced_repository_and_lifecycle_state(tmp_path: Path) -> None:
    document = _event_document("a" * 40, "b" * 40)
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    event_file = tmp_path / "event.json"

    pull_request["state"] = "closed"
    _write_event(event_file, document)
    with pytest.raises(InputError, match="state must be open"):
        load_pull_request_event(event_file)

    pull_request["state"] = "open"
    pull_request["draft"] = "false"
    _write_event(event_file, document)
    with pytest.raises(InputError, match="draft state must be a boolean"):
        load_pull_request_event(event_file)

    pull_request["draft"] = True
    document["action"] = "ready_for_review"
    _write_event(event_file, document)
    with pytest.raises(InputError, match="cannot describe a draft"):
        load_pull_request_event(event_file)

    pull_request["draft"] = False
    document["action"] = "opened"
    base = pull_request["base"]
    assert isinstance(base, dict)
    base["repo"] = {"id": 101, "full_name": "owner/repository"}
    _write_event(event_file, document)
    with pytest.raises(InputError, match="base repository does not match"):
        load_pull_request_event(event_file)


def test_action_context_is_bound_to_event_repository_and_refs(tmp_path: Path) -> None:
    repository, base, head = _repository(tmp_path)
    event_file = _write_event(tmp_path / "event.json", _event_document(base, head))
    assert check_pull_request(
        event_file,
        repository,
        expected_event_name="pull_request",
        expected_repository="owner/repository",
        expected_repository_id=100,
        expected_base_ref="main",
        expected_head_ref="feature",
    ).valid
    event = load_pull_request_event(event_file)

    with pytest.raises(InputError, match="must be pull_request"):
        validate_action_context(
            event,
            event_name="pull_request_target",
            repository="owner/repository",
            repository_id=100,
            base_ref="main",
            head_ref="feature",
        )
    with pytest.raises(InputError, match="repository does not match"):
        validate_action_context(
            event,
            event_name="pull_request",
            repository="lookalike/repository",
            repository_id=100,
            base_ref="main",
            head_ref="feature",
        )
    with pytest.raises(InputError, match="repository ID does not match"):
        validate_action_context(
            event,
            event_name="pull_request",
            repository="owner/repository",
            repository_id=101,
            base_ref="main",
            head_ref="feature",
        )
    with pytest.raises(InputError, match="base ref does not match"):
        validate_action_context(
            event,
            event_name="pull_request",
            repository="owner/repository",
            repository_id=100,
            base_ref="other",
            head_ref="feature",
        )
    with pytest.raises(InputError, match="head ref does not match"):
        validate_action_context(
            event,
            event_name="pull_request",
            repository="owner/repository",
            repository_id=100,
            base_ref="main",
            head_ref="other",
        )


def test_explicit_author_skip_applies_in_default_event_mode(tmp_path: Path) -> None:
    repo, base, head = _repository(tmp_path, head_message="invalid teh message")
    (repo / ".yaga.toml").write_text(
        '[commit]\nskip-pull-request-authors = ["renovate[bot]"]\ntypos = "check"\n',
        encoding="utf-8",
    )
    event = _write_event(
        tmp_path / "event.json",
        _event_document(
            base,
            head,
            title="invalid teh title",
            author={"login": "renovate[bot]", "type": "Bot", "id": 42},
        ),
    )
    report = check_pull_request(event, repo)
    assert report.skipped == 2
    assert all(
        result.skipped_reason == "Configured pull request author" for result in report.results
    )
