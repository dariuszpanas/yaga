"""Current PR descriptions reuse the local CLI policy and report contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commits.models import Diagnostic
from yaga.descriptions.service import check_description_source
from yaga.errors import InputError

runner = CliRunner()
LOCAL = ["pull-request", "description", "check"]
EVENT = ["github", "pull-request", "description", "check"]
URL = "https://github.com/example/project/pull/23"
BODY = "## Summary\nExplain the changed behavior.\n\n## Validation\nRan the parser tests.\n"


def _policy(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "description-policy.toml"
    path.write_text("description-policy-version = 1\n" + extra, encoding="utf-8")
    return path


def _fetch(monkeypatch: pytest.MonkeyPatch, body: str = BODY) -> list[object]:
    calls: list[object] = []

    def fetch(reference: object) -> SimpleNamespace:
        calls.append(reference)
        return SimpleNamespace(body=body, number=23, url=URL)

    monkeypatch.setattr("yaga.descriptions.service.fetch_pull_request_description", fetch)
    return calls


def _no_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("this invocation must not fetch a PR")

    monkeypatch.setattr("yaga.descriptions.service.fetch_pull_request_description", fail)


@pytest.mark.parametrize(
    "selector",
    [["--pr", URL], ["--pr", "23", "--repo", "example/project"]],
    ids=["url", "number-with-repository"],
)
def test_remote_description_reports_the_current_pr_identity_without_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: list[str]
) -> None:
    calls = _fetch(monkeypatch)
    policy = _policy(tmp_path)
    (tmp_path / ".git").write_text("not a Git repository", encoding="utf-8")
    (tmp_path / ".yaga.toml").write_text("not a discovered policy", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_REPO", "unselected/repository")
    result = runner.invoke(app, [*LOCAL, "--policy", str(policy), *selector, "--format", "json"])

    assert result.exit_code == 0, result.stderr
    assert len(calls) == 1
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["kind"] == "pull_request_description"
    assert report["status"] == "passed"
    assert report["source"] == URL
    assert report["pull_request_number"] == 23
    assert report["policy_path"] == str(policy.resolve())
    assert report["character_count"] == len(BODY)
    assert report["diagnostics"] == []
    assert "Explain the changed behavior" not in result.stdout


@pytest.mark.parametrize("source", ["file", "stdin", "event"])
def test_remote_descriptions_have_the_same_findings_as_other_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    body = "# Incorrect heading\n\nExplain \u200bthe change.\n\n## Validation\nTODO\n"
    _fetch(monkeypatch, body)
    policy = _policy(tmp_path, "min-words = 20\n")
    template = tmp_path / "template.md"
    template.write_text("## Summary\n\n## Validation\n", encoding="utf-8")
    shared = ["--policy", str(policy), "--template", str(template), "--format", "json"]
    remote = runner.invoke(app, [*LOCAL, *shared, "--pr", URL])
    if source == "file":
        body_file = tmp_path / "body.md"
        body_file.write_text(body, encoding="utf-8")
        local = runner.invoke(app, [*LOCAL, *shared, "--file", str(body_file)])
    elif source == "stdin":
        local = runner.invoke(app, [*LOCAL, *shared, "--stdin"], input=body)
    else:
        event_file = tmp_path / "event.json"
        event_file.write_text(
            json.dumps(
                {
                    "action": "edited",
                    "number": 23,
                    "pull_request": {
                        "number": 23,
                        "state": "open",
                        "draft": False,
                        "body": body,
                    },
                }
            ),
            encoding="utf-8",
        )
        local = runner.invoke(app, [*EVENT, *shared, "--event-file", str(event_file)])

    assert local.exit_code == remote.exit_code == 1
    local_report = json.loads(local.stdout)
    remote_report = json.loads(remote.stdout)
    for key in ("diagnostics", "diagnostic_count", "word_count", "character_count", "status"):
        assert local_report[key] == remote_report[key]
    assert remote_report["template_path"] == str(template)


@pytest.mark.parametrize(
    "selector",
    [
        ["--pr", URL, "--stdin"],
        ["--pr", URL, "--file", "missing.md"],
        ["--pr", URL, "--repo", "example/project"],
        ["--pr", "23"],
        ["--repo", "example/project", "--stdin"],
        ["--repo", "example/project", "--file", "missing.md"],
        ["--pr=-1", "--repo", "example/project"],
        ["--pr", "branch-name", "--repo", "example/project"],
    ],
    ids=[
        "pr-and-stdin",
        "pr-and-file",
        "url-and-repository",
        "number-without-repository",
        "repository-with-stdin",
        "repository-with-file",
        "negative-number",
        "implicit-branch",
    ],
)
def test_invalid_remote_source_selection_fails_before_loading_or_fetching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: list[str]
) -> None:
    _no_fetch(monkeypatch)
    result = runner.invoke(
        app,
        [*LOCAL, "--policy", str(tmp_path / "missing.toml"), *selector, "--format", "json"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["kind"] == "input"


def test_service_cannot_combine_a_live_pr_with_an_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_fetch(monkeypatch)
    with pytest.raises(InputError, match="exactly one"):
        check_description_source(
            policy_path=Path("missing.toml"),
            event_file=Path("event.json"),
            pull_request=URL,
        )


@pytest.mark.parametrize("failure", ["policy-missing", "policy-invalid", "template-missing"])
def test_configuration_inputs_are_loaded_before_a_remote_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _no_fetch(monkeypatch)
    policy = _policy(tmp_path)
    extra: list[str] = []
    if failure == "policy-missing":
        policy = tmp_path / "missing.toml"
    elif failure == "policy-invalid":
        policy.write_text("description-policy-version = 2\n", encoding="utf-8")
    else:
        extra = ["--template", str(tmp_path / "missing-template.md")]
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(policy), "--pr", URL, *extra, "--format", "json"]
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["status"] == "error"


@pytest.mark.parametrize("output_format", ["text", "json", "github"])
def test_fetch_failures_remain_operational_errors_even_when_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_format: str
) -> None:
    def fail(_reference: object) -> None:
        raise InputError("gh could not fetch the current PR description") from RuntimeError(
            "SENTINEL_PRIVATE_TRANSPORT_DETAIL"
        )

    monkeypatch.setattr("yaga.descriptions.service.fetch_pull_request_description", fail)
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(_policy(tmp_path)),
            "--pr",
            URL,
            "--quiet",
            "--format",
            output_format,
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "gh could not fetch" in result.stderr
    assert "SENTINEL_PRIVATE_TRANSPORT_DETAIL" not in result.stderr
    if output_format == "json":
        assert json.loads(result.stderr)["error"]["kind"] == "input"


@pytest.mark.parametrize(("body", "expected_exit"), [(BODY, 0), ("", 1)])
def test_remote_quiet_preserves_validation_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, expected_exit: int
) -> None:
    _fetch(monkeypatch, body)
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(_policy(tmp_path)), "--pr", URL, "--quiet"]
    )

    assert result.exit_code == expected_exit
    assert result.stdout == result.stderr == ""


def test_remote_source_keeps_unicode_and_literal_markdown_for_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "## Summary 👩‍💻\nExplain `None` and café safely.\n"
    _fetch(monkeypatch, body)
    result = runner.invoke(
        app, [*LOCAL, "--policy", str(_policy(tmp_path)), "--pr", URL, "--format", "json"]
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["character_count"] == len(body)


def test_requested_spelling_checks_run_on_the_fetched_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fetch(monkeypatch, "## Summary\n<!-- teh hidden -->\nteh change.\n")
    spelling_calls: list[tuple[str, bool]] = []

    def spelling(text: str, *, config_isolated: bool) -> tuple[Diagnostic, ...]:
        spelling_calls.append((text, config_isolated))
        return (Diagnostic("typos.word", "possible typo 'teh'; suggestions: the", 3, 1),)

    monkeypatch.setattr("yaga.descriptions.service.check_typos", spelling)
    result = runner.invoke(
        app,
        [
            *LOCAL,
            "--policy",
            str(_policy(tmp_path, 'typos = "check"\n')),
            "--pr",
            URL,
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1, result.stderr
    assert len(spelling_calls) == 1
    text, isolated = spelling_calls[0]
    assert isolated
    assert "teh hidden" not in text
    assert text.splitlines()[2] == "teh change."
    finding = json.loads(result.stdout)["diagnostics"][0]
    assert finding["code"] == "description.typo"
    assert finding["line"] == 3
    assert finding["column"] == 1


@pytest.mark.parametrize("source", ["file", "stdin", "event"])
def test_offline_sources_never_call_the_remote_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    _no_fetch(monkeypatch)
    policy = _policy(tmp_path)
    if source == "stdin":
        result = runner.invoke(app, [*LOCAL, "--policy", str(policy), "--stdin"], input=BODY)
    elif source == "file":
        body = tmp_path / "body.md"
        body.write_text(BODY, encoding="utf-8")
        result = runner.invoke(app, [*LOCAL, "--policy", str(policy), "--file", str(body)])
    else:
        event = tmp_path / "event.json"
        event.write_text(
            json.dumps(
                {
                    "action": "opened",
                    "number": 23,
                    "pull_request": {
                        "number": 23,
                        "state": "open",
                        "draft": False,
                        "body": BODY,
                    },
                }
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, [*EVENT, "--policy", str(policy), "--event-file", str(event)])

    assert result.exit_code == 0, result.stderr
