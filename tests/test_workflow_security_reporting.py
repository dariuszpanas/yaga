"""Tests for workflow-security text, JSON, and GitHub reports."""

from __future__ import annotations

import json

import pytest

from yaga.errors import InputError
from yaga.workflows.models import WorkflowDiagnostic, WorkflowOutputFormat
from yaga.workflows.security_models import (
    WORKFLOW_SECURITY_RULE_ORDER,
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityResult,
    WorkflowSecurityRule,
)
from yaga.workflows.security_reporting import (
    MAX_DIAGNOSTIC_CODE,
    MAX_GITHUB_ANNOTATIONS,
    render_workflow_security_error,
    render_workflow_security_report,
    workflow_security_report_document,
)


def diagnostic(
    code: str = "permissions.explicit",
    message: str = "workflow must define an explicit top-level permissions boundary",
    *,
    line: int = 3,
    column: int = 1,
) -> WorkflowDiagnostic:
    return WorkflowDiagnostic(code=code, message=message, line=line, column=column)


def report_with(
    *results: WorkflowSecurityResult,
    profile: WorkflowSecurityProfile = WorkflowSecurityProfile.RECOMMENDED_V1,
    rules: tuple[WorkflowSecurityRule, ...] = WORKFLOW_SECURITY_RULE_ORDER,
) -> WorkflowSecurityReport:
    return WorkflowSecurityReport(profile=profile, rules=rules, results=results)


def test_text_report_exposes_selection_and_bounds_untrusted_diagnostics() -> None:
    long_path = ".github/workflows/" + "p" * 1100 + "\u202e.yml"
    long_message = "unsafe\x1b\n" + "m" * 600
    report = report_with(
        WorkflowSecurityResult(path=".github/workflows/pass.yml", diagnostics=()),
        WorkflowSecurityResult(
            path=long_path,
            diagnostics=(diagnostic("permissions.explicit\x00", long_message, line=8, column=4),),
        ),
    )

    lines = render_workflow_security_report(report, WorkflowOutputFormat.TEXT).splitlines()

    assert lines[0] == (
        "Profile: recommended-v1; rules: permissions.explicit, "
        "permissions.top_level_write, permissions.write_all, secrets.inherit, "
        "checkout.untrusted_ref."
    )
    assert lines[1] == "PASSED  .github/workflows/pass.yml  0 diagnostic(s)"
    assert lines[2].startswith("FAILED  .github/workflows/")
    assert lines[2].endswith("…  1 diagnostic(s)")
    assert "\u202e" not in "\n".join(lines)
    assert lines[3].startswith("         [permissions.explicit?] line 8, column 4: unsafe??")
    assert lines[3].endswith("…")
    assert len(lines[3].split(": ", 1)[1]) == 500
    assert lines[4] == ("Checked 2 workflow file(s): 1 passed, 1 failed; 1 diagnostic(s).")


def test_json_report_has_exact_contract_and_no_raw_security_facts() -> None:
    report = report_with(
        WorkflowSecurityResult(
            path=".github/workflows/ci\n.yml",
            diagnostics=(diagnostic(message="bad\x00boundary", line=7, column=11),),
        ),
        WorkflowSecurityResult(path=".github/workflows/release.yml", diagnostics=()),
    )

    document = workflow_security_report_document(report)

    assert document == {
        "schema_version": 1,
        "kind": "github_workflow_security",
        "profile": "recommended-v1",
        "rules": [
            "permissions.explicit",
            "permissions.top_level_write",
            "permissions.write_all",
            "secrets.inherit",
            "checkout.untrusted_ref",
        ],
        "valid": False,
        "checked": 2,
        "passed": 1,
        "failed": 1,
        "diagnostics": 1,
        "workflows": [
            {
                "path": ".github/workflows/ci?.yml",
                "status": "failed",
                "valid": False,
                "diagnostics": [
                    {
                        "code": "permissions.explicit",
                        "message": "bad?boundary",
                        "line": 7,
                        "column": 11,
                    }
                ],
            },
            {
                "path": ".github/workflows/release.yml",
                "status": "passed",
                "valid": True,
                "diagnostics": [],
            },
        ],
    }
    serialized = json.dumps(document)
    assert "facts" not in serialized
    assert "triggers" not in serialized
    assert "permissions_value" not in serialized


def test_custom_rule_selection_serializes_custom_profile_and_canonical_rules() -> None:
    report = report_with(
        profile=WorkflowSecurityProfile.CUSTOM,
        rules=(
            WorkflowSecurityRule.SECRETS_INHERIT,
            WorkflowSecurityRule.CHECKOUT_UNTRUSTED_REF,
        ),
    )

    document = json.loads(render_workflow_security_report(report, WorkflowOutputFormat.JSON))

    assert document["profile"] == "custom"
    assert document["rules"] == ["secrets.inherit", "checkout.untrusted_ref"]
    assert render_workflow_security_report(report, WorkflowOutputFormat.TEXT).splitlines()[0] == (
        "Profile: custom; rules: secrets.inherit, checkout.untrusted_ref."
    )


def test_github_annotations_escape_properties_data_and_controls() -> None:
    report = report_with(
        WorkflowSecurityResult(
            path=".github/workflows/a%,:\r\n.yml",
            diagnostics=(
                diagnostic(
                    "permissions.%,:\r\n",
                    "bad%\r\nmessage\x00",
                    line=7,
                    column=5,
                ),
            ),
        )
    )

    annotation, summary = render_workflow_security_report(
        report, WorkflowOutputFormat.GITHUB
    ).splitlines()

    assert annotation == (
        "::error file=.github/workflows/a%25%2C%3A%0D%0A.yml,line=7,col=5,"
        "title=YAGA permissions.%25%2C%3A%0D%0A::bad%25%0D%0Amessage?"
    )
    assert summary == (
        "YAGA security checked 1 workflow file(s): 0 passed, 1 failed; 1 diagnostic(s)."
    )


def test_github_annotations_bound_properties_messages_and_titles() -> None:
    report = report_with(
        WorkflowSecurityResult(
            path="%," * 600,
            diagnostics=(diagnostic("security.%:" * 200, "%\r\n" * 200),),
        )
    )

    annotation = render_workflow_security_report(report, WorkflowOutputFormat.GITHUB).splitlines()[
        0
    ]
    properties, message = annotation.removeprefix("::error ").split("::", 1)
    path_property, _line, _column, title_property = properties.split(",")

    for value, maximum in (
        (path_property.removeprefix("file="), 1000),
        (title_property.removeprefix("title="), 120),
        (message, 500),
    ):
        assert len(value) <= maximum
        assert value.endswith("…")
        _assert_complete_github_escapes(value)


def test_github_report_caps_annotations_and_reports_omission() -> None:
    diagnostics = tuple(
        diagnostic(code=f"security.policy.{index}", message=f"failure {index}")
        for index in range(60)
    )
    report = report_with(
        WorkflowSecurityResult(path=".github/workflows/ci.yml", diagnostics=diagnostics)
    )

    lines = render_workflow_security_report(report, WorkflowOutputFormat.GITHUB).splitlines()
    annotations = [line for line in lines if line.startswith("::error")]

    assert len(annotations) == MAX_GITHUB_ANNOTATIONS
    assert "title=YAGA security.policy.48::failure 48" in annotations[-2]
    assert annotations[-1] == (
        "::error title=YAGA workflow security::"
        "11 additional workflow security diagnostic(s) omitted"
    )
    assert lines[-1] == (
        "YAGA security checked 1 workflow file(s): 0 passed, 1 failed; 60 diagnostic(s)."
    )


@pytest.mark.parametrize("output_format", list(WorkflowOutputFormat))
def test_operational_errors_are_sanitized_for_every_format(
    output_format: WorkflowOutputFormat,
) -> None:
    rendered = render_workflow_security_error(InputError("bad%\r\ninput\x00"), output_format)

    assert "\r" not in rendered
    if output_format is WorkflowOutputFormat.JSON:
        assert json.loads(rendered) == {
            "schema_version": 1,
            "error": {"kind": "input", "message": "bad%??input?"},
        }
    elif output_format is WorkflowOutputFormat.GITHUB:
        assert rendered == (
            "::error title=YAGA workflow security::YAGA input error: bad%25??input?"
        )
        assert len(rendered.splitlines()) == 1
    else:
        assert rendered == "YAGA input error: bad%??input?"


def test_empty_report_preserves_profile_rules_and_zero_aggregates() -> None:
    report = report_with()
    document = json.loads(render_workflow_security_report(report, WorkflowOutputFormat.JSON))

    assert document["profile"] == "recommended-v1"
    assert document["rules"] == [rule.value for rule in WORKFLOW_SECURITY_RULE_ORDER]
    assert document["valid"] is True
    assert document["checked"] == 0
    assert document["passed"] == 0
    assert document["failed"] == 0
    assert document["diagnostics"] == 0
    assert document["workflows"] == []


def test_diagnostic_code_is_bounded_in_text_and_json() -> None:
    code = "c" * (MAX_DIAGNOSTIC_CODE + 20)
    report = report_with(
        WorkflowSecurityResult(path="workflow.yml", diagnostics=(diagnostic(code=code),))
    )

    text = render_workflow_security_report(report, WorkflowOutputFormat.TEXT)
    document = workflow_security_report_document(report)

    assert f"[{'c' * 99}…]" in text
    assert document["workflows"][0]["diagnostics"][0]["code"] == f"{'c' * 99}…"


def _assert_complete_github_escapes(value: str) -> None:
    cursor = 0
    while (escape := value.find("%", cursor)) >= 0:
        encoded = value[escape + 1 : escape + 3]
        assert len(encoded) == 2
        assert all(character in "0123456789ABCDEF" for character in encoded)
        cursor = escape + 3
