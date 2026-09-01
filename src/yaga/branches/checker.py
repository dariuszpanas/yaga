"""Pure evaluation of one explicit branch name against branch policy."""

from __future__ import annotations

from yaga.branches.models import (
    BRANCH_ALLOWED_CODE,
    BRANCH_SYNTAX_CODE,
    BranchDiagnostic,
    BranchPolicy,
    BranchReport,
    _is_portable_branch_name,
    _validate_branch_transport,
)
from yaga.branches.patterns import match_branch_pattern
from yaga.errors import InputError


def check_branch_name(policy: BranchPolicy, branch: str) -> BranchReport:
    """Check one transport-bounded branch name and report at most one finding."""
    if not isinstance(policy, BranchPolicy):
        raise TypeError("policy must be a BranchPolicy")
    if not isinstance(branch, str):
        raise TypeError("branch must be a string")
    try:
        _validate_branch_transport(branch)
    except ValueError as error:
        raise InputError(str(error)) from error

    if not _is_portable_branch_name(branch):
        return BranchReport(
            policy=policy,
            branch=branch,
            matched_pattern=None,
            diagnostics=(
                BranchDiagnostic(
                    code=BRANCH_SYNTAX_CODE,
                    message="branch name does not use the portable YAGA branch syntax",
                ),
            ),
        )

    for pattern in policy.allowed_patterns:
        if match_branch_pattern(pattern, branch):
            return BranchReport(
                policy=policy,
                branch=branch,
                matched_pattern=pattern,
                diagnostics=(),
            )

    return BranchReport(
        policy=policy,
        branch=branch,
        matched_pattern=None,
        diagnostics=(
            BranchDiagnostic(
                code=BRANCH_ALLOWED_CODE,
                message="branch name is not allowed by any configured pattern",
            ),
        ),
    )
