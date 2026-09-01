"""Tests for bounded branch-policy text, JSON, and GitHub reports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yaga.branches.checker import check_branch_name
from yaga.branches.models import (
    BRANCH_ALLOWED_CODE,
    BRANCH_SYNTAX_CODE,
    BranchPolicy,
)
from yaga.branches.reporting import (
    MAX_TEXT_PATTERNS,
    BranchOutputFormat,
    render_branch_error,
    render_branch_report,
)
from yaga.branches.service import CheckedBranch
from yaga.errors import ConfigurationError


def _checked(
    tmp_path: Path,
    branch: str,
    *patterns: str,
) -> CheckedBranch:
    policy = BranchPolicy(
        branch_policy_version=1,
        allowed_patterns=patterns or ("main", "feat/*"),
    )
    return CheckedBranch(
        report=check_branch_name(policy, branch),
        policy_path=(tmp_path / "branch-policy.toml").resolve(),
    )


def test_json_pass_report_has_the_exact_stable_schema(tmp_path: Path) -> None:
    checked = _checked(tmp_path, "feat/installed-layer")

    document = json.loads(render_branch_report(checked, BranchOutputFormat.JSON))

    assert document == {
        "schema_version": 1,
        "kind": "branch_policy",
        "status": "passed",
        "valid": True,
        "policy_path": str((tmp_path / "branch-policy.toml").resolve()),
        "branch": "feat/installed-layer",
        "allowed_patterns": ["main", "feat/*"],
        "matched_pattern": "feat/*",
        "diagnostics": [],
    }


@pytest.mark.parametrize(
    ("branch", "code", "message"),
    [
        (
            "chore/installed-layer",
            BRANCH_ALLOWED_CODE,
            "branch name is not allowed by any configured pattern",
        ),
        (
            "refs/heads/main",
            BRANCH_SYNTAX_CODE,
            "branch name does not use the portable YAGA branch syntax",
        ),
    ],
)
def test_json_failure_has_one_diagnostic_and_no_match(
    tmp_path: Path,
    branch: str,
    code: str,
    message: str,
) -> None:
    checked = _checked(tmp_path, branch)

    document = json.loads(render_branch_report(checked, BranchOutputFormat.JSON))

    assert document["status"] == "failed"
    assert document["valid"] is False
    assert document["matched_pattern"] is None
    assert document["diagnostics"] == [{"code": code, "message": message}]


def test_text_report_is_deterministic_and_bounds_the_allowed_preview(tmp_path: Path) -> None:
    patterns = tuple(f"type-{index}/*" for index in range(MAX_TEXT_PATTERNS + 2))
    checked = _checked(tmp_path, "chore/installed-layer", *patterns)

    rendered = render_branch_report(checked, BranchOutputFormat.TEXT)

    assert rendered.startswith(
        f"Policy: {(tmp_path / 'branch-policy.toml').resolve()}\n"
        "Branch: chore/installed-layer\n"
        "FAILED  [branch.allowed] branch name is not allowed by any configured pattern\n"
    )
    assert "type-7/* … (+2 more)" in rendered
    assert "type-8/*" not in rendered
    assert rendered.endswith("Branch policy: failed.")


def test_text_pass_reports_the_first_configured_match(tmp_path: Path) -> None:
    checked = _checked(tmp_path, "feat/topic", "feat/**", "feat/*")

    rendered = render_branch_report(checked, BranchOutputFormat.TEXT)

    assert "Matched: feat/**" in rendered
    assert "FAILED" not in rendered
    assert rendered.endswith("Branch policy: passed.")


def test_github_failure_is_one_escaped_annotation_plus_summary(tmp_path: Path) -> None:
    checked = _checked(tmp_path, "feat/bad%,:name")

    annotation, summary = render_branch_report(
        checked,
        BranchOutputFormat.GITHUB,
    ).splitlines()

    assert annotation == (
        "::error title=YAGA branch.syntax::branch 'feat/bad%25,:name': "
        "branch name does not use the portable YAGA branch syntax"
    )
    assert summary == "YAGA branch policy: failed for branch 'feat/bad%,:name'."


def test_github_pass_is_a_summary_without_an_annotation(tmp_path: Path) -> None:
    checked = _checked(tmp_path, "main")

    rendered = render_branch_report(checked, BranchOutputFormat.GITHUB)

    assert rendered == "YAGA branch policy: passed; branch 'main' matched 'main'."
    assert not rendered.startswith("::")


def test_invalid_branch_controls_are_sanitized_in_every_report(tmp_path: Path) -> None:
    checked = _checked(tmp_path, "feat/zero\u200bwidth\nname")

    text = render_branch_report(checked, BranchOutputFormat.TEXT)
    document = json.loads(render_branch_report(checked, BranchOutputFormat.JSON))
    github = render_branch_report(checked, BranchOutputFormat.GITHUB)

    for rendered in (text, document["branch"], github):
        assert "\u200b" not in rendered
        assert "\nname" not in rendered
    assert document["branch"] == "feat/zero?width?name"


@pytest.mark.parametrize("output_format", list(BranchOutputFormat))
def test_operational_errors_are_structured_sanitized_and_bounded(
    output_format: BranchOutputFormat,
) -> None:
    error = ConfigurationError("bad%\x1b[31m\r\n" + "x" * 1000)

    rendered = render_branch_error(error, output_format)

    assert "\x1b" not in rendered
    assert "\r" not in rendered
    if output_format is BranchOutputFormat.JSON:
        document = json.loads(rendered)
        assert document["schema_version"] == 1
        assert document["kind"] == "branch_policy"
        assert document["status"] == "error"
        assert document["valid"] is False
        assert document["error"]["kind"] == "configuration"
        assert len(document["error"]["message"]) <= 500
    elif output_format is BranchOutputFormat.GITHUB:
        assert rendered.startswith("::error title=YAGA branch policy::")
        assert "%25" in rendered
        assert len(rendered.removeprefix("::error title=YAGA branch policy::")) <= 500
    else:
        assert rendered.startswith("YAGA configuration error: bad%?")
