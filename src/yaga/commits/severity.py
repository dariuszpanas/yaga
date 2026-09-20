"""Closed, opt-in severity choices for configurable policy findings."""

from dataclasses import replace

from yaga.commits.models import CheckResult, CommitPolicy, DiagnosticSeverity

# Structural/resource/selection failures deliberately cannot be demoted.
WARNING_RULES = frozenset(
    {
        "type.allowed",
        "type.case",
        "scope.required",
        "scope.forbidden",
        "scope.allowed",
        "scope.case",
        "header.length",
        "description.length",
        "description.case",
        "description.ending",
        "breaking.marker-pair",
        "body.required",
        "body.forbidden",
        "body.length",
        "body.word-count",
        "body.line-length",
        "body.paragraph-format",
        "footer.line-length",
        "footer.required-colon",
        "footer.required",
        "footer.forbidden",
        "footer.value",
        "reference.required",
        "typos.word",
    }
)


def apply_severity(result: CheckResult, policy: CommitPolicy) -> CheckResult:
    """Assign severity from policy only, preserving diagnostic order and identity."""
    if not policy.warning_rules:
        return result
    warnings = set(policy.warning_rules) & WARNING_RULES
    return replace(
        result,
        diagnostics=tuple(
            replace(
                d,
                severity=DiagnosticSeverity.WARNING
                if d.code in warnings
                else DiagnosticSeverity.ERROR,
            )
            for d in result.diagnostics
        ),
    )
