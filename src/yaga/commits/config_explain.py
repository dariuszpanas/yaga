"""Bounded explanations of effective type-specific commit policy."""

import re
import unicodedata
from typing import Any

from yaga.commits.models import CommitPolicy, MessageFormat
from yaga.commits.policy import resolve_presence_policy
from yaga.errors import InputError


def explain_type(policy: CommitPolicy, commit_type: str) -> dict[str, Any]:
    """Explain overrides without claiming that a complete message would pass."""
    if policy.message_format is MessageFormat.PLAIN:
        raise InputError("plain message-format has no commit type to explain")
    if re.fullmatch(r"[^\s()!:]{1,128}", commit_type) is None or any(
        unicodedata.category(char).startswith("C") for char in commit_type
    ):
        raise InputError("configuration type must be one safe type token of at most 128 characters")
    scope, scope_key = resolve_presence_policy(
        policy.scope_policy, policy.scope_policy_by_type, commit_type
    )
    body, body_key = resolve_presence_policy(
        policy.body_policy, policy.body_policy_by_type, commit_type
    )
    return {
        "type": commit_type,
        "listed_type_allowed": policy.allowed_types is None
        or commit_type.casefold() in {key.casefold() for key in policy.allowed_types},
        "scope_policy": scope.value,
        "scope_source": f"scope-policy-by-type.{scope_key}" if scope_key else "scope-policy",
        "body_policy": body.value,
        "body_source": f"body-policy-by-type.{body_key}" if body_key else "body-policy",
        "note": "Body policy applies to full messages only; all other configured rules still apply.",
    }
