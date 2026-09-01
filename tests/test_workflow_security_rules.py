"""Focused tests for pure workflow-security rule evaluation."""

from __future__ import annotations

import pytest

from yaga.workflows.security_models import WorkflowSecurityRule
from yaga.workflows.security_rules import RECOMMENDED_V1_RULES, evaluate_workflow_security
from yaga.workflows.yaml import parse_workflow_bundle

PIN = "a" * 40


def _diagnostics(raw: str, rules: tuple[WorkflowSecurityRule, ...] = RECOMMENDED_V1_RULES):
    facts = parse_workflow_bundle(raw.encode(), label="workflow.yml").security
    return evaluate_workflow_security(facts, rules)


def _codes(raw: str, rules: tuple[WorkflowSecurityRule, ...] = RECOMMENDED_V1_RULES):
    return [diagnostic.code for diagnostic in _diagnostics(raw, rules)]


def test_recommended_policy_requires_explicit_top_level_permissions() -> None:
    diagnostics = _diagnostics("jobs: {}\n")

    assert [(item.code, item.line, item.column) for item in diagnostics] == [
        ("security.permissions.explicit", 1, 1)
    ]


def test_explicit_empty_permissions_satisfy_the_presence_rule() -> None:
    assert _codes("permissions: {}\njobs: {}\n") == []


def test_top_level_mapping_write_is_rejected_but_job_scoped_write_is_allowed() -> None:
    raw = """\
permissions:
  contents: read
  statuses: write
jobs:
  publish:
    permissions:
      statuses: write
    steps: []
"""

    diagnostics = _diagnostics(raw)

    assert [(item.code, item.line, item.column) for item in diagnostics] == [
        ("security.permissions.top_level_write", 3, 13)
    ]


def test_write_all_is_rejected_at_top_level_without_a_duplicate_write_diagnostic() -> None:
    assert _codes("permissions: write-all\njobs: {}\n") == ["security.permissions.write_all"]


def test_write_all_is_rejected_at_job_level() -> None:
    raw = """\
permissions: {}
jobs:
  publish:
    permissions: write-all
    steps: []
"""

    assert _codes(raw) == ["security.permissions.write_all"]


def test_reusable_workflow_calls_must_enumerate_secrets() -> None:
    raw = f"""\
permissions: {{}}
jobs:
  call:
    uses: owner/repository/.github/workflows/called.yml@{PIN}
    secrets: inherit
"""

    assert _codes(raw) == ["security.secrets.inherit"]


def test_inherit_spelling_outside_a_reusable_call_is_not_selected() -> None:
    raw = """\
permissions: {}
jobs:
  ordinary:
    secrets: inherit
    steps: []
"""

    assert _codes(raw) == []


def test_explicit_reusable_workflow_secret_mapping_passes() -> None:
    raw = f"""\
permissions: {{}}
jobs:
  call:
    uses: owner/repository/.github/workflows/called.yml@{PIN}
    secrets:
      token: ${{{{ secrets.TOKEN }}}}
"""

    assert _codes(raw) == []


@pytest.mark.parametrize(
    "trigger",
    [
        "pull_request_target",
        "[push, pull_request_target]",
        "{pull_request_target: {types: [opened]}}",
    ],
)
@pytest.mark.parametrize(
    ("input_name", "value"),
    [
        ("ref", "${{ github.event.pull_request.head.sha }}"),
        ("ref", "${{ github.event.pull_request.head.ref }}"),
        ("ref", "${{ github.head_ref }}"),
        ("repository", "${{ github.event.pull_request.head.repo.full_name }}"),
        ("allow-unsafe-pr-checkout", "true"),
        ("REF", "${{ github.event.pull_request.head.sha }}"),
        ("Allow-Unsafe-PR-Checkout", "true"),
        ("ref", "${{ github['event']['pull_request']['head']['sha'] }}"),
        (
            "repository",
            "${{ github['event']['pull_request']['head']['repo']['full_name'] }}",
        ),
        ("allow-unsafe-pr-checkout", "${{ true }}"),
        ("ref", "${{ github.sha }}"),
        ("allow-unsafe-pr-checkout", "{}"),
        ("allow-unsafe-pr-checkout", "[false]"),
        ("allow-unsafe-pr-checkout", "FALSE"),
        ("allow-unsafe-pr-checkout", "' false '"),
    ],
)
def test_pull_request_target_rejects_untrusted_checkout_inputs(
    trigger: str,
    input_name: str,
    value: str,
) -> None:
    raw = f"""\
on: {trigger}
permissions: {{}}
jobs:
  privileged:
    steps:
      - uses: actions/checkout@{PIN}
        with:
          {input_name}: {value}
"""

    assert _codes(raw) == ["security.checkout.untrusted_ref"]


@pytest.mark.parametrize(
    ("input_name", "value"),
    [
        ("ref", "${{ github.event.workflow_run.head_sha }}"),
        ("ref", "${{ github.event.workflow_run.head_branch }}"),
        ("repository", "${{ github.event.workflow_run.head_repository.full_name }}"),
        ("allow-unsafe-pr-checkout", "TRUE"),
        ("ref", "${{ github['event']['workflow_run']['head_sha'] }}"),
        ("allow-unsafe-pr-checkout", "${{ true }}"),
    ],
)
def test_workflow_run_rejects_upstream_checkout_inputs(input_name: str, value: str) -> None:
    raw = f"""\
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
permissions: {{}}
jobs:
  privileged:
    steps:
      - uses: ACTIONS/CHECKOUT@{PIN}
        with:
          {input_name}: {value}
"""

    assert _codes(raw) == ["security.checkout.untrusted_ref"]


@pytest.mark.parametrize(
    ("trigger", "action", "input_name", "value"),
    [
        (
            "pull_request",
            f"actions/checkout@{PIN}",
            "ref",
            "${{ github.event.pull_request.head.sha }}",
        ),
        ("pull_request_target", f"actions/checkout@{PIN}", "ref", "main"),
        (
            "pull_request_target",
            f"actions/checkout@{PIN}",
            "ref",
            "github.event.pull_request.head.sha",
        ),
        (
            "pull_request_target",
            f"owner/checkout@{PIN}",
            "ref",
            "${{ github.event.pull_request.head.sha }}",
        ),
        ("workflow_run", f"actions/checkout@{PIN}", "allow-unsafe-pr-checkout", "false"),
        (
            "pull_request_target",
            f"actions/checkout@{PIN}",
            "Allow-Unsafe-PR-Checkout",
            "false",
        ),
    ],
)
def test_safe_or_unrelated_checkout_shapes_pass(
    trigger: str,
    action: str,
    input_name: str,
    value: str,
) -> None:
    raw = f"""\
on: {trigger}
permissions: {{}}
jobs:
  check:
    steps:
      - uses: {action}
        with:
          {input_name}: {value}
"""

    assert _codes(raw) == []


def test_checkout_rule_reports_once_per_checkout_uses_occurrence() -> None:
    raw = f"""\
on: pull_request_target
permissions: {{}}
jobs:
  check:
    steps:
      - uses: actions/checkout@{PIN}
        with:
          ref: ${{{{ github.event.pull_request.head.sha }}}}
          repository: ${{{{ github.event.pull_request.head.repo.full_name }}}}
          allow-unsafe-pr-checkout: true
"""

    assert _codes(raw) == ["security.checkout.untrusted_ref"]


def test_exact_custom_rule_selection_does_not_run_unselected_rules() -> None:
    raw = "permissions: write-all\njobs: {}\n"

    assert _codes(raw, (WorkflowSecurityRule.PERMISSIONS_EXPLICIT,)) == []
