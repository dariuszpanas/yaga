"""Proposed PR messages use complete policy without changing title-only checks."""

import json
from pathlib import Path

import pytest

from tests.test_github_pull_request import _event_document, _repository, _write_event
from yaga.commits.config import load_config
from yaga.commits.github_event import check_pull_request, load_pull_request_event
from yaga.commits.github_reporting import PullRequestOutputFormat, render_pull_request_report
from yaga.errors import ConfigurationError, GitError, InputError


def _fixture(tmp_path: Path, *, mode: str = "title-and-body", extra: str = ""):
    repo, base, head = _repository(
        tmp_path, head_message="fix: correct behavior\n\nExplain the correction"
    )
    (repo / ".yaga.toml").write_text(
        f'[commit]\npull-request-message="{mode}"\nbody-policy="required"\n' + extra,
        encoding="utf-8",
    )
    document = _event_document(base, head)
    return repo, document


def test_body_edit_rechecks_proposed_message_without_new_commit(tmp_path: Path) -> None:
    repo, document = _fixture(tmp_path)
    event = _write_event(tmp_path / "event.json", document)
    first = check_pull_request(event, repo)
    assert first.title.valid and first.commits[0].valid
    assert first.proposed_message is not None
    assert [d.code for d in first.proposed_message.diagnostics] == ["body.required"]
    document["action"] = "edited"
    document["pull_request"]["body"] = "Explain why this change is needed"
    _write_event(event, document)
    second = check_pull_request(event, repo)
    assert second.valid
    assert first.event.head_sha == second.event.head_sha
    report = json.loads(render_pull_request_report(second, PullRequestOutputFormat.JSON))
    assert report["checked"] == 3
    assert report["pull_request"]["proposed_message"]["status"] == "passed"
    assert "MESSAGE" in render_pull_request_report(second, PullRequestOutputFormat.TEXT)
    assert "one proposed message" in render_pull_request_report(
        second, PullRequestOutputFormat.GITHUB
    )


def test_default_report_has_no_proposed_message(tmp_path: Path) -> None:
    repo, document = _fixture(tmp_path, mode="title-only")
    report = check_pull_request(_write_event(tmp_path / "event.json", document), repo)
    assert report.valid and report.proposed_message is None
    data = json.loads(render_pull_request_report(report, PullRequestOutputFormat.JSON))
    assert data["checked"] == 2
    assert "proposed_message" not in data["pull_request"]


@pytest.mark.parametrize("body", [False, 123, [], {}, "\ud800"])
def test_malformed_body_rejected_before_author_skip(tmp_path: Path, body: object) -> None:
    repo, document = _fixture(tmp_path, extra='skip-pull-request-authors=["octocat"]\n')
    document["pull_request"]["body"] = body
    with pytest.raises(InputError, match="body"):
        check_pull_request(_write_event(tmp_path / "event.json", document), repo)


@pytest.mark.parametrize("body", [None, "", " ", "\n"])
def test_empty_body_cannot_satisfy_required_prose(tmp_path: Path, body: object) -> None:
    repo, document = _fixture(tmp_path)
    document["pull_request"]["body"] = body
    report = check_pull_request(_write_event(tmp_path / "event.json", document), repo)
    assert report.proposed_message is not None
    assert "body.required" in {d.code for d in report.proposed_message.diagnostics}


def test_skip_covers_proposed_message_but_cannot_hide_stale_head(tmp_path: Path) -> None:
    repo, document = _fixture(tmp_path, extra='skip-pull-request-authors=["octocat"]\n')
    event = _write_event(tmp_path / "event.json", document)
    assert check_pull_request(event, repo).skipped == 3
    document["pull_request"]["head"]["sha"] = "a" * 40
    with pytest.raises(GitError, match="HEAD"):
        check_pull_request(_write_event(event, document), repo)


@pytest.mark.parametrize("value", ['"automatic"', "true", "1", "[]", "{}"])
def test_mode_is_closed(tmp_path: Path, value: str) -> None:
    config = tmp_path / ".yaga.toml"
    config.write_text(f"[commit]\npull-request-message={value}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="pull-request-message"):
        load_config(config)


def test_final_footer_does_not_count_as_body(tmp_path: Path) -> None:
    repo, document = _fixture(tmp_path, extra='required-footer-tokens=["Refs"]\n')
    document["pull_request"]["body"] = "Refs: #123"
    report = check_pull_request(_write_event(tmp_path / "event.json", document), repo)
    assert report.proposed_message is not None
    assert {d.code for d in report.proposed_message.diagnostics} == {"body.required"}


def test_body_utf8_byte_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yaga.commits import github_event

    document = _event_document("a" * 40, "b" * 40, title="fix: x")
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["body"] = "é" * 10
    event = _write_event(tmp_path / "event.json", document)
    monkeypatch.setattr(github_event, "MAX_MESSAGE_BYTES", 28)
    assert load_pull_request_event(event).body == "é" * 10
    monkeypatch.setattr(github_event, "MAX_MESSAGE_BYTES", 27)
    with pytest.raises(InputError, match="byte limit"):
        load_pull_request_event(event)


@pytest.mark.parametrize("body,expected", [("Explain the correction", 0), (None, 1), (False, 2)])
@pytest.mark.parametrize("trusted", [False, True])
def test_installed_and_dependency_free_entrypoints(
    tmp_path: Path, body: object, expected: int, trusted: bool
) -> None:
    import os
    import subprocess
    import sys

    from tests.test_commit_action import ROOT, _action_fixture, _commit, _git

    config = '[commit]\npull-request-message="title-and-body"\nbody-policy="required"\n'
    repo, event_path, context = _action_fixture(
        tmp_path, config=config, head_message="fix: correct behavior\n\nExplain the correction"
    )
    event = json.loads(event_path.read_text(encoding="utf-8"))
    event["action"] = "edited"
    event["pull_request"]["body"] = body
    if trusted:
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "fix: correct behavior\n\nExplain the correction")
        _git(repo, "update-ref", "refs/remotes/pull/17/head", head)
        _git(repo, "checkout", "--detach", base)
        # Working-tree policy cannot disable the proposed-message rule.
        (repo / ".yaga.toml").write_text("[commit]\n", encoding="utf-8")
        event["repository"]["default_branch"] = "main"
        event["pull_request"]["base"]["sha"] = base
        event["pull_request"]["head"]["sha"] = head
        context.update(
            GITHUB_EVENT_NAME="pull_request_target",
            GITHUB_SHA=base,
            GITHUB_REF="refs/heads/main",
            YAGA_COMMIT_TRUSTED_CONFIG=".yaga.toml",
        )
    event_path.write_text(json.dumps(event), encoding="utf-8")
    environment = dict(os.environ, **context, PYTHONPATH=str(ROOT / "src"))
    environment.pop("YAGA_ACTION_RUNTIME", None)
    command = [
        "-m",
        "yaga",
        "github",
        "pull-request",
        "check",
        "--event-file",
        str(event_path),
        "--repo",
        str(repo),
        "--format",
        "github",
    ]
    for action in (False, True):
        if trusted and not action:
            continue  # Trusted runner policy belongs to the Action boundary.
        environment.pop("YAGA_COMMIT_ACTION_RUNTIME", None)
        flags = []
        if action:
            environment["YAGA_COMMIT_ACTION_RUNTIME"] = "1"
            flags = ["-P", "-S"]
        result = subprocess.run(
            [sys.executable, *flags, *command],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        if expected == 1:
            assert "proposed message" in result.stdout and "body.required" in result.stdout
