"""Tests for pull-request text, JSON, and GitHub annotation reports."""

from __future__ import annotations

import json

from yaga.commits.github_event import PullRequestEvent, PullRequestValidationReport
from yaga.commits.github_reporting import (
    MAX_GITHUB_ANNOTATIONS,
    PullRequestOutputFormat,
    _workflow_data,
    render_pull_request_error,
    render_pull_request_report,
)
from yaga.commits.models import CheckResult, CommitTarget, Diagnostic
from yaga.errors import InputError


def _report(
    *,
    diagnostics: tuple[Diagnostic, ...] = (),
    skipped_reason: str | None = None,
    author_login: str = "octocat",
    author_id: int = 1,
    author_type: str = "User",
) -> PullRequestValidationReport:
    event = PullRequestEvent(
        action="opened",
        number=17,
        title="feat: check pull requests",
        base_sha="a" * 40,
        head_sha="b" * 40,
        base_ref="main",
        head_ref="feature",
        repository="owner/repository",
        repository_id=100,
        draft=False,
        author_login=author_login,
        author_id=author_id,
        author_type=author_type,
    )
    title = CheckResult(
        target=CommitTarget(label="pull request #17 title", message=event.title),
        header=event.title,
        skipped_reason=skipped_reason,
    )
    commit = CheckResult(
        target=CommitTarget(label="commit", message="bad", sha="b" * 40),
        header="bad",
        diagnostics=diagnostics,
        skipped_reason=skipped_reason,
    )
    return PullRequestValidationReport(
        event=event,
        title=title,
        commits=(commit,),
        config_path=None,
    )


def test_text_and_json_reports_keep_title_separate_from_commits() -> None:
    report = _report(diagnostics=(Diagnostic(code="syntax.header", message="expected header"),))

    text = render_pull_request_report(report, PullRequestOutputFormat.TEXT)
    document = json.loads(render_pull_request_report(report, PullRequestOutputFormat.JSON))

    assert "Pull request #17" in text
    assert "TITLE  PASSED" in text
    assert "COMMIT FAILED" in text
    assert document["schema_version"] == 1
    assert document["valid"] is False
    assert document["pull_request"]["title"]["source"] == "pull request #17 title"
    assert document["pull_request"]["author"] == {
        "login": "octocat",
        "id": 1,
        "type": "User",
    }
    assert len(document["pull_request"]["commits"]) == 1


def test_dependabot_skip_reason_is_visible_in_every_report_format() -> None:
    report = _report(
        skipped_reason="Dependabot pull request",
        author_login="dependabot[bot]",
        author_id=49_699_333,
        author_type="Bot",
    )

    text = render_pull_request_report(report, PullRequestOutputFormat.TEXT)
    document = json.loads(render_pull_request_report(report, PullRequestOutputFormat.JSON))
    github = render_pull_request_report(report, PullRequestOutputFormat.GITHUB)

    assert text.count("skipped: Dependabot pull request") == 2
    assert document["valid"] is True
    assert document["failed"] == 0
    assert document["skipped"] == 2
    assert document["pull_request"]["author"] == {
        "login": "dependabot[bot]",
        "id": 49_699_333,
        "type": "Bot",
    }
    assert document["pull_request"]["title"]["skipped_reason"] == "Dependabot pull request"
    assert document["pull_request"]["commits"][0]["skipped_reason"] == ("Dependabot pull request")
    assert github.endswith("2 skipped. Skip reason: Dependabot pull request.")


def test_github_report_escapes_commands_and_bounds_annotation_count() -> None:
    diagnostics = tuple(
        Diagnostic(code=f"policy.{index}", message="bad % value\r\n::warning:: injected")
        for index in range(MAX_GITHUB_ANNOTATIONS + 3)
    )

    rendered = render_pull_request_report(
        _report(diagnostics=diagnostics),
        PullRequestOutputFormat.GITHUB,
    )
    lines = rendered.splitlines()

    annotations = [line for line in lines if line.startswith("::error")]
    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "%25" in annotations[0]
    assert "%0D%0A" not in annotations[0]
    assert all(not line.startswith("::warning") for line in lines)
    assert "additional policy diagnostic(s) omitted" in annotations[-1]
    assert (
        lines[-1]
        == "YAGA checked pull request #17: one title and 1 commit(s); 1 failed, 0 skipped."
    )


def test_github_operational_error_is_one_escaped_annotation() -> None:
    rendered = render_pull_request_error(
        InputError("bad % path\n::warning:: injected"),
        PullRequestOutputFormat.GITHUB,
    )

    assert rendered.startswith("::error title=YAGA commit policy::YAGA input error:")
    assert "%25" in rendered
    assert "\n" not in rendered
    assert not rendered.startswith("::warning")
    assert _workflow_data("bad%\r\nvalue") == "bad%25%0D%0Avalue"
