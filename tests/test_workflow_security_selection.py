"""Tests for exact workflow-security rule selection."""

from __future__ import annotations

from typing import Any

import pytest

from yaga.errors import InputError
from yaga.workflows.security_models import WorkflowSecurityProfile, WorkflowSecurityRule
from yaga.workflows.security_rules import (
    RECOMMENDED_V1_RULES,
    RECOMMENDED_V2_RULES,
    RECOMMENDED_V3_RULES,
    normalize_security_rules,
)


def test_default_and_explicit_recommended_profile_select_canonical_rules() -> None:
    default = normalize_security_rules()
    explicit = normalize_security_rules(profile="recommended-v1")

    assert default == explicit
    assert default.profile is WorkflowSecurityProfile.RECOMMENDED_V1
    assert default.rules == RECOMMENDED_V1_RULES


def test_recommended_v2_is_explicit_and_adds_checkout_credential_policy() -> None:
    selection = normalize_security_rules(profile="recommended-v2")

    assert selection.profile is WorkflowSecurityProfile.RECOMMENDED_V2
    assert selection.rules == RECOMMENDED_V2_RULES
    assert selection.rules[:-1] == RECOMMENDED_V1_RULES
    assert selection.rules[-1] is WorkflowSecurityRule.CHECKOUT_PERSIST_CREDENTIALS


def test_recommended_v3_is_explicit_and_adds_pull_request_write_policy() -> None:
    selection = normalize_security_rules(profile="recommended-v3")

    assert selection.profile is WorkflowSecurityProfile.RECOMMENDED_V3
    assert selection.rules == RECOMMENDED_V3_RULES
    assert selection.rules[:-1] == RECOMMENDED_V2_RULES
    assert selection.rules[-1] is WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE


def test_custom_rules_are_exact_and_canonical_regardless_argument_order() -> None:
    selection = normalize_security_rules(
        rules=(
            "permissions.pull_request_write",
            "checkout.persist_credentials",
            "secrets.inherit",
            "permissions.explicit",
        )
    )

    assert selection.profile is WorkflowSecurityProfile.CUSTOM
    assert selection.rules == (
        WorkflowSecurityRule.PERMISSIONS_EXPLICIT,
        WorkflowSecurityRule.SECRETS_INHERIT,
        WorkflowSecurityRule.CHECKOUT_PERSIST_CREDENTIALS,
        WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"profile": "recommended-v1", "rules": ("permissions.explicit",)},
            "either a workflow security profile or explicit rules",
        ),
        ({"profile": "custom"}, "custom workflow security profile requires explicit rules"),
        ({"profile": "RECOMMENDED-V1"}, "unknown workflow security profile"),
        ({"rules": ("permissions.EXPLICIT",)}, "unknown workflow security rule"),
        (
            {"rules": ("permissions.explicit", "permissions.explicit")},
            "must not be repeated",
        ),
    ],
)
def test_invalid_profile_and_rule_selections_fail_closed(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(InputError, match=message):
        normalize_security_rules(**kwargs)


def test_rule_selection_is_hard_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yaga.workflows.security_rules.MAX_SECURITY_RULES", 1)

    with pytest.raises(InputError, match="hard 1-rule limit"):
        normalize_security_rules(rules=("permissions.explicit", "secrets.inherit"))
