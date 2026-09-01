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
from yaga.errors import GitError, InputError


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "YAGA Tests")
    _git(repository, "config", "user.email", "yaga@example.invalid")
    base = _commit(repository, "chore: establish baseline", "base")
    head = _commit(repository, "feat(cli): add pull request checks", "head")
    return repository, base, head


def _commit(repository: Path, message: str, content: str) -> str:
    (repository / "content.txt").write_text(content, encoding="utf-8")
    _git(repository, "add", "--", "content.txt")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _event_document(
    base: str, head: str, *, title: object = "feat(cli): check pull requests"
) -> dict[str, object]:
    return {
        "action": "synchronize",
        "number": 17,
        "repository": {"id": 100, "full_name": "owner/repository"},
        "pull_request": {
            "number": 17,
            "title": title,
            "state": "open",
            "draft": False,
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


def test_check_rejects_a_checkout_that_is_not_the_event_head(tmp_path: Path) -> None:
    repository, base, event_head = _repository(tmp_path)
    _commit(repository, "fix: move checkout", "later checkout state")
    event_file = _write_event(tmp_path / "event.json", _event_document(base, event_head))

    with pytest.raises(GitError, match="HEAD does not match"):
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
