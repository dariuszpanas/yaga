"""Versioned YAGA TOML policy schemas, starter generation, interactive selection, and auditing."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yaga.policies.audit import PolicyAuditEntry, PolicyAuditReport, audit_policies
    from yaga.policies.init_templates import (
        InitializedPolicyItem,
        PolicyInitReport,
        existing_policy_status,
        initialize_policies,
        render_starter_toml,
    )
    from yaga.policies.schemas import POLICY_SPECS, PolicyKind, PolicySpec

__all__ = [
    "InitializedPolicyItem",
    "POLICY_SPECS",
    "PolicyAuditEntry",
    "PolicyAuditReport",
    "PolicyInitReport",
    "PolicyKind",
    "PolicySpec",
    "audit_policies",
    "existing_policy_status",
    "initialize_policies",
    "render_starter_toml",
]

_EXPORTS_BY_NAME: dict[str, str] = {
    "PolicyAuditEntry": "yaga.policies.audit",
    "PolicyAuditReport": "yaga.policies.audit",
    "audit_policies": "yaga.policies.audit",
    "InitializedPolicyItem": "yaga.policies.init_templates",
    "PolicyInitReport": "yaga.policies.init_templates",
    "existing_policy_status": "yaga.policies.init_templates",
    "initialize_policies": "yaga.policies.init_templates",
    "render_starter_toml": "yaga.policies.init_templates",
    "POLICY_SPECS": "yaga.policies.schemas",
    "PolicyKind": "yaga.policies.schemas",
    "PolicySpec": "yaga.policies.schemas",
}


def __getattr__(name: str) -> object:
    module_name = _EXPORTS_BY_NAME.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
