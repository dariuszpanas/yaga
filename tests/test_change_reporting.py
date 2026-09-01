"""Tests for bounded changed-path text, JSON, and GitHub reports."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from yaga.changes.models import (
    ChangeReport,
    ChangeRule,
    ChangeRuleResult,
    ChangeSelection,
)
from yaga.changes.reporting import (
    CHANGE_REQUIRE_ANY_CODE,
    MAX_GITHUB_ANNOTATIONS,
    MAX_JSON_PATHS,
    MAX_JSON_RULE_PATHS,
    ChangeOutputFormat,
    render_change_error,
    render_change_report,
)
from yaga.changes.service import CheckedChanges
from yaga.errors import ConfigurationError


def _selection(paths: tuple[str, ...]) -> ChangeSelection:
    return ChangeSelection(
        revision_range="a" * 40 + "..." + "b" * 40,
        base_sha="a" * 40,
        head_sha="b" * 40,
        comparison_sha="c" * 40,
        paths=paths,
    )


def _rule(name: str = "source-needs-tests") -> ChangeRule:
    return ChangeRule(
        name=name,
        when_any=("src/**/*.py",),
        require_any=("tests/**/*.py", "checks/$%,:*.py"),
    )


def _checked(
    tmp_path: Path,
    *,
    paths: tuple[str, ...],
    results: tuple[ChangeRuleResult, ...],
) -> CheckedChanges:
    return CheckedChanges(
        report=ChangeReport(selection=_selection(paths), results=results),
        policy_path=(tmp_path / "policy.toml").resolve(),
    )


def test_json_report_has_stable_schema_counts_codes_and_bounded_previews(
    tmp_path: Path,
) -> None:
    paths = tuple(f"src/file-{index:03}.py" for index in range(70))
    result = ChangeRuleResult(
        rule=_rule(),
        triggered_paths=paths[:40],
        required_paths=(),
    )
    checked = _checked(tmp_path, paths=paths, results=(result,))

    document = json.loads(render_change_report(checked, ChangeOutputFormat.JSON))

    assert document == {
        "schema_version": 1,
        "kind": "change_policy",
        "status": "failed",
        "valid": False,
        "policy_path": str((tmp_path / "policy.toml").resolve()),
        "range": "a" * 40 + "..." + "b" * 40,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "comparison_sha": "c" * 40,
        "paths_changed": 70,
        "paths": list(paths[:MAX_JSON_PATHS]),
        "paths_omitted": 70 - MAX_JSON_PATHS,
        "rules": [
            {
                "name": "source-needs-tests",
                "status": "failed",
                "code": CHANGE_REQUIRE_ANY_CODE,
                "when_any": ["src/**/*.py"],
                "require_any": ["tests/**/*.py", "checks/$%,:*.py"],
                "triggered_paths": list(paths[:MAX_JSON_RULE_PATHS]),
                "triggered_paths_omitted": 40 - MAX_JSON_RULE_PATHS,
                "required_paths": [],
                "required_paths_omitted": 0,
            }
        ],
        "passed": 0,
        "failed": 1,
        "skipped": 0,
    }


def test_json_codes_are_null_for_passed_and_skipped_rules(tmp_path: Path) -> None:
    paths = ("docs/guide.md", "src/main.py", "tests/test_main.py")
    passed_rule = _rule("source-needs-tests")
    skipped_rule = ChangeRule(
        name="docs-need-news",
        when_any=("docs/**/*.md",),
        require_any=("news/*.md",),
    )
    checked = _checked(
        tmp_path,
        paths=paths,
        results=(
            ChangeRuleResult(
                rule=passed_rule,
                triggered_paths=("src/main.py",),
                required_paths=("tests/test_main.py",),
            ),
            ChangeRuleResult(rule=skipped_rule, triggered_paths=(), required_paths=()),
        ),
    )

    rules = json.loads(render_change_report(checked, ChangeOutputFormat.JSON))["rules"]

    assert [(item["status"], item["code"]) for item in rules] == [
        ("passed", None),
        ("skipped", None),
    ]


def test_text_report_is_deterministic_and_caps_each_path_preview(tmp_path: Path) -> None:
    paths = tuple(f"src/file-{index:02}.py" for index in range(10))
    checked = _checked(
        tmp_path,
        paths=paths,
        results=(ChangeRuleResult(rule=_rule(), triggered_paths=paths, required_paths=()),),
    )

    rendered = render_change_report(checked, ChangeOutputFormat.TEXT)

    assert rendered.startswith(
        f"Policy: {(tmp_path / 'policy.toml').resolve()}\n"
        f"Range: {'a' * 40}...{'b' * 40}\n"
        "FAILED  source-needs-tests\n"
    )
    assert (
        "[change.require_any] no changed path matched require-any: "
        "tests/**/*.py, checks/$%,:*.py" in rendered
    )
    assert "src/file-07.py … (+2 more)" in rendered
    assert "src/file-08.py" not in rendered
    assert rendered.endswith(
        "Change policy: 1 rule(s); 0 passed, 1 failed, 0 skipped; 10 changed path(s)."
    )


def test_github_report_uses_one_escaped_annotation_per_failed_rule(tmp_path: Path) -> None:
    path = "src/a%,:file.py"
    checked = _checked(
        tmp_path,
        paths=(path,),
        results=(ChangeRuleResult(rule=_rule(), triggered_paths=(path,), required_paths=()),),
    )

    annotation, summary = render_change_report(
        checked,
        ChangeOutputFormat.GITHUB,
    ).splitlines()

    assert annotation == (
        "::error file=src/a%25%2C%3Afile.py,title=YAGA change.require_any::"
        "rule 'source-needs-tests' requires at least one changed path matching: "
        "tests/**/*.py, checks/$%25,:*.py"
    )
    assert summary == (
        "YAGA change policy: 1 rule(s); 0 passed, 1 failed, 0 skipped; 1 changed path(s)."
    )


def test_github_report_defensively_caps_annotations_and_reserves_omission() -> None:
    path = "src/main.py"
    results = tuple(
        ChangeRuleResult(
            rule=ChangeRule(
                name=f"rule-{index}",
                when_any=("src/**/*.py",),
                require_any=("tests/**/*.py",),
            ),
            triggered_paths=(path,),
            required_paths=(),
        )
        for index in range(40)
    )
    fake_report = cast(
        ChangeReport,
        SimpleNamespace(
            results=results,
            selection=SimpleNamespace(paths=(path,)),
            passed=0,
            failed=40,
            skipped=0,
            valid=False,
        ),
    )
    checked = CheckedChanges(report=fake_report, policy_path=Path("C:/policy.toml"))

    lines = render_change_report(checked, ChangeOutputFormat.GITHUB).splitlines()
    annotations = [line for line in lines if line.startswith("::error")]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "YAGA change.require_any" in annotations[-2]
    assert annotations[-1] == (
        "::error title=YAGA change policy::9 additional changed-path violation(s) omitted"
    )


@pytest.mark.parametrize("output_format", list(ChangeOutputFormat))
def test_operational_errors_are_structured_sanitized_and_bounded(
    output_format: ChangeOutputFormat,
) -> None:
    error = ConfigurationError("bad%\x1b[31m\r\n" + "x" * 1000)

    rendered = render_change_error(error, output_format)

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    if output_format is ChangeOutputFormat.JSON:
        document = json.loads(rendered)
        assert document["schema_version"] == 1
        assert document["kind"] == "change_policy"
        assert document["status"] == "error"
        assert document["error"]["kind"] == "configuration"
        assert len(document["error"]["message"]) <= 500
    elif output_format is ChangeOutputFormat.GITHUB:
        assert rendered.startswith("::error title=YAGA change policy::")
        assert "%25" in rendered
        assert len(rendered.removeprefix("::error title=YAGA change policy::")) <= 500
    else:
        assert rendered.startswith("YAGA configuration error: bad%?")
