"""Tests for stable, sanitized Conventional Commit reports."""

from __future__ import annotations

import json

from yaga.commits.checker import check_target
from yaga.commits.models import (
    CommitPolicy,
    CommitTarget,
    LoadedConfig,
    OutputFormat,
    ValidationReport,
)
from yaga.commits.reporting import render_config, render_error, render_report, safe_text
from yaga.errors import ConfigurationError


def test_text_report_groups_diagnostics_and_summary() -> None:
    result = check_target(CommitTarget(label="message", message="not conventional"), CommitPolicy())
    report = ValidationReport(results=(result,), config_path=None)

    rendered = render_report(report, OutputFormat.TEXT)

    assert "FAILED" in rendered
    assert "[syntax.header]" in rendered
    assert "1 failed" in rendered


def test_json_report_has_a_versioned_machine_contract() -> None:
    result = check_target(CommitTarget(label="message", message="feat: add output"), CommitPolicy())
    report = ValidationReport(results=(result,), config_path=None)

    document = json.loads(render_report(report, OutputFormat.JSON))

    assert document["schema_version"] == 1
    assert document["valid"] is True
    assert document["commits"][0]["status"] == "passed"
    assert document["commits"][0]["diagnostics"] == []


def test_body_word_count_diagnostic_is_stable_in_text_and_json_reports() -> None:
    result = check_target(
        CommitTarget(label="message", message="feat: document the rule\n\none two"),
        CommitPolicy(body_min_words=3),
    )
    report = ValidationReport(results=(result,), config_path=None)

    rendered = render_report(report, OutputFormat.TEXT)
    document = json.loads(render_report(report, OutputFormat.JSON))

    assert "[body.word-count] line 3: body has 2 words; minimum is 3" in rendered
    assert document["commits"][0]["diagnostics"] == [
        {
            "code": "body.word-count",
            "message": "body has 2 words; minimum is 3",
            "line": 3,
            "column": 1,
        }
    ]


def test_config_reports_the_effective_body_word_minimum() -> None:
    loaded = LoadedConfig(policy=CommitPolicy(body_min_words=7), path=None)

    rendered = render_config(loaded, OutputFormat.TEXT)
    document = json.loads(render_config(loaded, OutputFormat.JSON))

    assert "body-min-words: 7" in rendered
    assert document["config"]["body_min_words"] == 7


def test_untrusted_headers_and_operational_errors_are_sanitized() -> None:
    assert safe_text("feat: hello\x1b[31m\nworld\u202e\u0085") == ("feat: hello?[31m?world??")
    rendered = render_error(
        ConfigurationError("bad\x1b[31m\nconfiguration"),
        OutputFormat.JSON,
    )
    document = json.loads(rendered)
    assert document["error"] == {
        "kind": "configuration",
        "message": "bad?[31m?configuration",
    }


def test_text_and_json_reports_bound_every_untrusted_string(tmp_path) -> None:
    token = "\u009b" + "x" * 1000
    result = check_target(
        CommitTarget(label=f"{tmp_path}\u202e", message=f"{token}: description"),
        CommitPolicy(allowed_types=("feat",)),
    )
    report = ValidationReport(results=(result,), config_path=tmp_path / "policy\u202e.toml")

    document = json.loads(render_report(report, OutputFormat.JSON))

    assert "\u009b" not in document["commits"][0]["source"]
    assert "\u202e" not in document["config_path"]
    assert len(document["commits"][0]["diagnostics"][0]["message"]) <= 500
    assert "\u009b" not in document["commits"][0]["diagnostics"][0]["message"]


def test_config_text_sanitizes_paths_and_values(tmp_path) -> None:
    loaded = LoadedConfig(
        policy=CommitPolicy(ignored_headers=("\u009b31m*",)),
        path=tmp_path / "policy\u202e.toml",
    )

    rendered = render_config(loaded, OutputFormat.TEXT)

    assert "\u009b" not in rendered
    assert "\u202e" not in rendered
