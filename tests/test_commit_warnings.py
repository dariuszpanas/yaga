"""Opt-in warnings use versioned reports without weakening input boundaries."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga.cli import app
from yaga.commits.checker import check_target
from yaga.commits.config import load_config
from yaga.commits.github_reporting import CommitOutputFormat, render_commit_report
from yaga.commits.models import (
    CommitPolicy,
    CommitTarget,
    Diagnostic,
    DiagnosticSeverity,
    ValidationReport,
)
from yaga.commits.service import check_commits
from yaga.commits.typos import apply_typos
from yaga.errors import ConfigurationError, InputError


def _config(tmp_path: Path, *, extra: str = "") -> Path:
    path = tmp_path / ".yaga.toml"
    path.write_text(
        '[commit]\nbody-policy="required"\nwarning-rules=["body.required"]\n' + extra,
        encoding="utf-8",
    )
    return path


def test_warning_only_passes_and_mixed_findings_still_fail(tmp_path: Path) -> None:
    path = _config(tmp_path)
    report = check_commits(tmp_path, message="fix: correct behavior", config=path)
    assert report.valid and report.passed == 1 and report.failed == 0
    assert report.warning_count == 1 and report.schema_version == 2
    diagnostic = report.results[0].diagnostics[0]
    assert diagnostic.code == "body.required" and diagnostic.severity is DiagnosticSeverity.WARNING
    mixed = check_commits(
        tmp_path, message="fix: x", config=_config(tmp_path, extra="description-min-length=5\n")
    )
    assert not mixed.valid and mixed.failed == 1 and mixed.warning_count == 1
    document = json.loads(render_commit_report(mixed, CommitOutputFormat.JSON))
    assert document["schema_version"] == 2 and document["warning_count"] == 1
    assert {d["severity"] for d in document["commits"][0]["diagnostics"]} == {"error", "warning"}
    github = render_commit_report(mixed, CommitOutputFormat.GITHUB)
    assert "::warning title=" in github and "::error title=" in github
    text = render_commit_report(report, CommitOutputFormat.TEXT)
    assert "[warning] [body.required]" in text and "Warnings: 1" in text


def test_schema_two_is_selected_even_without_findings(tmp_path: Path) -> None:
    report = check_commits(tmp_path, message="fix: x\n\nExplain why", config=_config(tmp_path))
    document = json.loads(render_commit_report(report, CommitOutputFormat.JSON))
    assert document["schema_version"] == 2 and document["warning_count"] == 0
    assert document["valid"]


def test_default_schema_keeps_severity_implicit(tmp_path: Path) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text('[commit]\nbody-policy="required"\n', encoding="utf-8")
    report = check_commits(tmp_path, message="fix: x", config=path)
    document = json.loads(render_commit_report(report, CommitOutputFormat.JSON))
    assert document["schema_version"] == 1 and "warning_count" not in document
    assert "severity" not in document["commits"][0]["diagnostics"][0]
    assert not report.valid


@pytest.mark.parametrize(
    "value",
    [
        '["syntax.header"]',
        '["syntax.separator"]',
        '["message.size"]',
        '["merge.rejected"]',
        '["unknown"]',
        '["body.required", "body.required"]',
        "true",
        "[1]",
    ],
)
def test_unsupported_or_structural_warning_rules_are_rejected(tmp_path: Path, value: str) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text("[commit]\nwarning-rules=" + value, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="warning-rules"):
        load_config(path)


def test_direct_policy_cannot_demote_structural_failures() -> None:
    policy = CommitPolicy(warning_rules=("syntax.header",))
    result = check_target(CommitTarget("message", "not conventional"), policy)
    assert not result.valid and result.diagnostics[0].severity is DiagnosticSeverity.ERROR


def test_warning_cli_exit_codes_and_operational_error_envelope(tmp_path: Path) -> None:
    path = _config(tmp_path)
    runner = CliRunner()
    base = ["commit", "check", "--config", str(path), "--format", "json"]
    assert runner.invoke(app, [*base, "--message", "fix: x"]).exit_code == 0
    assert runner.invoke(app, [*base, "--message", "not conventional"]).exit_code == 1
    invalid = runner.invoke(app, [*base, "--file", str(tmp_path / "missing")])
    assert invalid.exit_code == 2
    document = json.loads(invalid.output)
    assert document["schema_version"] == 1 and "error" in document


def test_typos_warning_does_not_hide_tool_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yaga.commits.models import TyposPolicy

    policy = CommitPolicy(typos=TyposPolicy.CHECK, warning_rules=("typos.word",))
    result = check_target(CommitTarget("message", "fix: x"), policy)
    monkeypatch.setattr(
        "yaga.commits.typos.check_typos",
        lambda *args, **kwargs: (Diagnostic("typos.word", "spelling finding"),),
    )
    checked = apply_typos(result, policy, repository=tmp_path)
    assert checked.valid and checked.diagnostics[0].severity is DiagnosticSeverity.WARNING

    def fail(*args, **kwargs):
        raise InputError("tool unavailable")

    monkeypatch.setattr("yaga.commits.typos.check_typos", fail)
    with pytest.raises(InputError):
        apply_typos(result, policy, repository=tmp_path)


def test_annotation_truncation_never_hides_error_severity() -> None:
    result = check_target(CommitTarget("message", "fix: x"), CommitPolicy())
    warnings = tuple(
        Diagnostic(
            "body.length", "warning %\n::error injected", severity=DiagnosticSeverity.WARNING
        )
        for _ in range(50)
    )
    result = replace(result, diagnostics=(*warnings, Diagnostic("syntax.header", "error")))
    report = ValidationReport((result,), None, report_version=2)
    rendered = render_commit_report(report, CommitOutputFormat.GITHUB)
    lines = rendered.splitlines()
    assert len([line for line in lines if line.startswith("::")]) == 50
    assert lines[49].startswith("::error") and "2 additional" in lines[49]
    assert "%25" in lines[0] and "\n::error injected" not in rendered


def test_pr_warning_report_and_author_skip_schema(tmp_path: Path) -> None:
    from tests.test_github_pull_request import _event_document, _repository, _write_event
    from yaga.commits.github_event import check_pull_request
    from yaga.commits.github_reporting import PullRequestOutputFormat, render_pull_request_report

    repo, base, head = _repository(tmp_path, head_message="fix: correct behavior")
    config = _config(repo, extra='pull-request-message="title-and-body"\n')
    event = _write_event(tmp_path / "event.json", _event_document(base, head))
    report = check_pull_request(event, repo)
    assert report.valid and report.warning_count == 2
    document = json.loads(render_pull_request_report(report, PullRequestOutputFormat.JSON))
    assert document["schema_version"] == 2 and document["warning_count"] == 2
    assert document["pull_request"]["proposed_message"]["diagnostics"][0]["severity"] == "warning"
    github = render_pull_request_report(report, PullRequestOutputFormat.GITHUB)
    assert "::warning" in github and "::error" not in github
    config.write_text(
        config.read_text(encoding="utf-8") + 'skip-pull-request-authors=["octocat"]\n',
        encoding="utf-8",
    )
    skipped = check_pull_request(event, repo)
    assert skipped.schema_version == 2 and skipped.skipped == 3 and skipped.warning_count == 0


def test_empty_selection_keeps_opted_in_report_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yaga.commits import service

    monkeypatch.setattr(service, "_select_targets", lambda **kwargs: [])
    report = check_commits(tmp_path, config=_config(tmp_path))
    assert report.schema_version == 2 and report.warning_count == 0 and report.valid


def test_repository_warnings_preserve_nested_schema_and_error_precedence() -> None:
    from yaga.repository.models import (
        RepositoryCheckResult,
        RepositoryCheckStatus,
        RepositoryOutputFormat,
        RepositoryProvider,
        RepositoryReport,
    )
    from yaga.repository.reporting import render_repository_report

    result = check_target(CommitTarget("message", "fix: x"), CommitPolicy())
    warnings = tuple(
        Diagnostic("body.length", "warning", severity=DiagnosticSeverity.WARNING) for _ in range(60)
    )
    result = replace(result, diagnostics=warnings)
    child = ValidationReport((result,), None, report_version=2)
    check = RepositoryCheckResult(RepositoryProvider.COMMIT, report=child)
    aggregate = RepositoryReport((check,))
    assert aggregate.valid and aggregate.passed == 1
    rendered = render_repository_report(aggregate, RepositoryOutputFormat.GITHUB)
    assert "::warning" in rendered and "::error" not in rendered
    document = json.loads(render_repository_report(aggregate, RepositoryOutputFormat.JSON))
    assert document["schema_version"] == 1
    assert document["checks"][0]["report"]["schema_version"] == 2
    errored = RepositoryReport(
        (check, RepositoryCheckResult(RepositoryProvider.WORKFLOW, error=InputError("unavailable")))
    )
    assert errored.status is RepositoryCheckStatus.ERROR
    assert "::error" in render_repository_report(errored, RepositoryOutputFormat.GITHUB)
