"""Tests for immutable workflow-security result models."""

from __future__ import annotations

import pytest

from yaga.workflows.models import WorkflowDiagnostic
from yaga.workflows.security_models import (
    WorkflowSecurityProfile,
    WorkflowSecurityReport,
    WorkflowSecurityResult,
    WorkflowSecurityRule,
)


def test_security_report_aggregates_stable_outcomes() -> None:
    diagnostic = WorkflowDiagnostic(
        code="security.permissions.explicit",
        message="workflow must declare top-level permissions",
        line=1,
        column=1,
    )
    report = WorkflowSecurityReport(
        profile=WorkflowSecurityProfile.CUSTOM,
        rules=(WorkflowSecurityRule.PERMISSIONS_EXPLICIT,),
        results=(
            WorkflowSecurityResult(path=".github/workflows/pass.yml", diagnostics=()),
            WorkflowSecurityResult(
                path=".github/workflows/fail.yml",
                diagnostics=(diagnostic,),
            ),
        ),
    )

    assert report.checked == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.diagnostics == 1
    assert report.valid is False
    assert [result.status for result in report.results] == ["passed", "failed"]


@pytest.mark.parametrize(
    ("profile", "rules", "message"),
    [
        (WorkflowSecurityProfile.CUSTOM, (), "must be nonempty"),
        (
            WorkflowSecurityProfile.CUSTOM,
            (
                WorkflowSecurityRule.SECRETS_INHERIT,
                WorkflowSecurityRule.PERMISSIONS_EXPLICIT,
            ),
            "unique and canonical",
        ),
        (
            WorkflowSecurityProfile.RECOMMENDED_V1,
            (WorkflowSecurityRule.PERMISSIONS_EXPLICIT,),
            "complete canonical rule set",
        ),
    ],
)
def test_security_report_rejects_invalid_rule_invariants(
    profile: WorkflowSecurityProfile,
    rules: tuple[WorkflowSecurityRule, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        WorkflowSecurityReport(profile=profile, rules=rules, results=())


def test_empty_custom_result_set_is_valid_with_one_explicit_rule() -> None:
    report = WorkflowSecurityReport(
        profile=WorkflowSecurityProfile.CUSTOM,
        rules=(WorkflowSecurityRule.PERMISSIONS_EXPLICIT,),
        results=(),
    )

    assert report.valid is True
    assert report.checked == report.passed == report.failed == report.diagnostics == 0
