"""Tests for strict, explicit repository-check plans."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from yaga.errors import ConfigurationError
from yaga.repository.models import RepositoryCheckPlan, RepositoryProvider
from yaga.repository.plan import MAX_REPOSITORY_PLAN_BYTES, load_repository_check_plan
from yaga.workflows.inputs import (
    MAX_WORKFLOW_FILES,
    MAX_WORKFLOW_PATH_BYTES,
    MAX_WORKFLOW_PATH_COMPONENTS,
)
from yaga.workflows.security_models import (
    WorkflowSecurityProfile,
    WorkflowSecurityRule,
)
from yaga.workflows.security_rules import MAX_SECURITY_RULES


def write_plan(path: Path, body: str) -> Path:
    plan = path / "yaga-plan.toml"
    plan.write_text(body, encoding="utf-8")
    return plan


def plan_document(*lines: str) -> str:
    return "\n".join(("plan-version = 1", *lines, ""))


def test_minimal_plan_requires_only_a_version_and_explicit_checks(tmp_path: Path) -> None:
    path = write_plan(tmp_path, plan_document('checks = ["commit"]'))

    plan = load_repository_check_plan(path)

    assert plan == RepositoryCheckPlan(
        plan_version=1,
        checks=(RepositoryProvider.COMMIT,),
    )
    assert plan.workflow_paths == ()
    assert plan.workflow_security_profile is None
    assert plan.workflow_security_rules == ()


def test_plan_normalizes_provider_rule_order_and_portable_paths(tmp_path: Path) -> None:
    path = write_plan(
        tmp_path,
        plan_document(
            'checks = ["workflow-lint", "workflow-security", "commit", "workflow"]',
            'workflow-paths = [".github/workflows", ".github/workflows/ci.yml"]',
            "workflow-security-rules = [",
            '  "permissions.pull_request_write",',
            '  "permissions.explicit",',
            "]",
        ),
    )

    plan = load_repository_check_plan(path)

    assert plan.checks == tuple(RepositoryProvider)
    assert plan.workflow_paths == (
        PurePosixPath(".github/workflows"),
        PurePosixPath(".github/workflows/ci.yml"),
    )
    assert plan.workflow_security_profile is None
    assert plan.workflow_security_rules == (
        WorkflowSecurityRule.PERMISSIONS_EXPLICIT,
        WorkflowSecurityRule.PERMISSIONS_PULL_REQUEST_WRITE,
    )


def test_named_security_profile_is_preserved_without_expanding_rules(tmp_path: Path) -> None:
    path = write_plan(
        tmp_path,
        plan_document(
            'checks = ["workflow-security"]',
            'workflow-security-profile = "recommended-v3"',
        ),
    )

    plan = load_repository_check_plan(path)

    assert plan.workflow_security_profile is WorkflowSecurityProfile.RECOMMENDED_V3
    assert plan.workflow_security_rules == ()


@pytest.mark.parametrize(
    ("lines", "expected_checks", "expected_profile", "expected_rules"),
    [
        (
            ('checks = ["commit"]',),
            (RepositoryProvider.COMMIT,),
            None,
            (),
        ),
        (
            (
                'checks = ["workflow-security"]',
                'workflow-security-profile = "recommended-v2"',
            ),
            (RepositoryProvider.WORKFLOW_SECURITY,),
            WorkflowSecurityProfile.RECOMMENDED_V2,
            (),
        ),
        (
            (
                'checks = ["workflow-security"]',
                'workflow-security-rules = ["permissions.explicit"]',
            ),
            (RepositoryProvider.WORKFLOW_SECURITY,),
            None,
            (WorkflowSecurityRule.PERMISSIONS_EXPLICIT,),
        ),
    ],
)
def test_explicit_loader_selection_matrix(
    tmp_path: Path,
    lines: tuple[str, ...],
    expected_checks: tuple[RepositoryProvider, ...],
    expected_profile: WorkflowSecurityProfile | None,
    expected_rules: tuple[WorkflowSecurityRule, ...],
) -> None:
    plan = load_repository_check_plan(write_plan(tmp_path, plan_document(*lines)))

    assert plan.checks == expected_checks
    assert plan.workflow_security_profile is expected_profile
    assert plan.workflow_security_rules == expected_rules


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('checks = ["commit"]\n', "missing plan-version"),
        ("plan-version = 1\n", "missing checks"),
        ('plan-version = 2\nchecks = ["commit"]\n', "integer 1"),
        ('plan-version = true\nchecks = ["commit"]\n', "integer 1"),
        ('plan-version = "1"\nchecks = ["commit"]\n', "integer 1"),
        (plan_document("checks = []"), "must not be empty"),
        (plan_document('checks = "commit"'), "array of strings"),
        (plan_document('checks = ["commit", 1]'), "array of strings"),
        (plan_document('checks = ["unknown"]'), "unknown repository check provider"),
        (plan_document('checks = ["commit", "commit"]'), "must not be repeated"),
        (
            plan_document(
                'checks = ["commit", "workflow", "workflow-security", "workflow-lint", "commit"]'
            ),
            "checks exceeds 4 entries",
        ),
    ],
)
def test_required_plan_contract_is_strict(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    path = write_plan(tmp_path, body)

    with pytest.raises(ConfigurationError, match=message):
        load_repository_check_plan(path)


@pytest.mark.parametrize(
    "key",
    ["commit", "range", "config", "format", "include", "environment", "expression"],
)
def test_runtime_and_expansion_keys_are_not_plan_keys(tmp_path: Path, key: str) -> None:
    path = write_plan(
        tmp_path,
        plan_document('checks = ["commit"]', f'{key} = "unsupported"'),
    )

    with pytest.raises(ConfigurationError, match=rf"unknown repository check plan key.*{key}"):
        load_repository_check_plan(path)


def test_plan_loader_never_discovers_another_file(tmp_path: Path) -> None:
    write_plan(tmp_path, plan_document('checks = ["commit"]'))
    missing = tmp_path / "another-plan.toml"

    with pytest.raises(ConfigurationError, match="cannot read repository check plan"):
        load_repository_check_plan(missing)


def test_workflow_paths_remain_repo_relative_not_plan_directory_relative(
    tmp_path: Path,
) -> None:
    plan_directory = tmp_path / "configuration" / "nested"
    plan_directory.mkdir(parents=True)
    path = write_plan(
        plan_directory,
        plan_document(
            'checks = ["workflow"]',
            'workflow-paths = [".github/workflows/ci.yml"]',
        ),
    )

    plan = load_repository_check_plan(path)

    assert plan.workflow_paths == (PurePosixPath(".github/workflows/ci.yml"),)
    assert plan.workflow_paths[0] != PurePosixPath("configuration/nested/.github/workflows/ci.yml")


def test_plan_accepts_its_exact_byte_limit_and_rejects_one_more_byte(tmp_path: Path) -> None:
    prefix = plan_document('checks = ["commit"]').encode() + b"#"
    exact = prefix + b"x" * (MAX_REPOSITORY_PLAN_BYTES - len(prefix))
    path = tmp_path / "yaga-plan.toml"
    path.write_bytes(exact)

    assert load_repository_check_plan(path).checks == (RepositoryProvider.COMMIT,)

    path.write_bytes(exact + b"x")
    with pytest.raises(ConfigurationError, match=rf"exceeds {MAX_REPOSITORY_PLAN_BYTES} bytes"):
        load_repository_check_plan(path)


def test_invalid_utf8_and_toml_are_configuration_errors(tmp_path: Path) -> None:
    path = tmp_path / "yaga-plan.toml"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="not valid UTF-8"):
        load_repository_check_plan(path)

    path.write_text("plan-version = [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid repository check plan TOML"):
        load_repository_check_plan(path)


def test_utf8_bom_is_accepted_without_changing_plan_semantics(tmp_path: Path) -> None:
    path = tmp_path / "yaga-plan.toml"
    path.write_bytes(b"\xef\xbb\xbf" + plan_document('checks = ["commit"]').encode())

    assert load_repository_check_plan(path).checks == (RepositoryProvider.COMMIT,)


@pytest.mark.parametrize(
    "workflow_path",
    [
        "",
        "/.github/workflows/ci.yml",
        "C:/.github/workflows/ci.yml",
        ".github\\workflows\\ci.yml",
        ".github//workflows/ci.yml",
        "./.github/workflows/ci.yml",
        ".github/workflows/../ci.yml",
        "~/.github/workflows/ci.yml",
        ".github/workflows/NUL.yml",
        ".github/workflows/bad:name.yml",
        ".github/workflows/trailing.",
        ".github/workflows/ leading.yml",
        ".github/workflows/zero\u200bwidth.yml",
        "a/" * MAX_WORKFLOW_PATH_COMPONENTS + "ci.yml",
        "a" * (MAX_WORKFLOW_PATH_BYTES + 1),
    ],
)
def test_workflow_paths_are_canonical_portable_and_literal(
    tmp_path: Path,
    workflow_path: str,
) -> None:
    encoded = json.dumps(workflow_path)
    path = write_plan(
        tmp_path,
        plan_document(
            'checks = ["workflow"]',
            f"workflow-paths = [{encoded}]",
        ),
    )

    with pytest.raises(ConfigurationError, match="workflow-paths contains"):
        load_repository_check_plan(path)


def test_workflow_paths_are_bounded_unique_and_provider_scoped(tmp_path: Path) -> None:
    explicitly_empty = write_plan(
        tmp_path,
        plan_document('checks = ["workflow"]', "workflow-paths = []"),
    )
    with pytest.raises(ConfigurationError, match="workflow-paths must not be empty"):
        load_repository_check_plan(explicitly_empty)

    duplicate = write_plan(
        tmp_path,
        plan_document(
            'checks = ["workflow"]',
            'workflow-paths = [".github/workflows/CI.yml", ".github/workflows/ci.yml"]',
        ),
    )
    with pytest.raises(ConfigurationError, match="duplicate path"):
        load_repository_check_plan(duplicate)

    entries = ", ".join(
        f'".github/workflows/{index}.yml"' for index in range(MAX_WORKFLOW_FILES + 1)
    )
    over_limit = write_plan(
        tmp_path,
        plan_document('checks = ["workflow"]', f"workflow-paths = [{entries}]"),
    )
    with pytest.raises(ConfigurationError, match=rf"exceeds {MAX_WORKFLOW_FILES} entries"):
        load_repository_check_plan(over_limit)

    unused = write_plan(
        tmp_path,
        plan_document(
            'checks = ["commit"]',
            'workflow-paths = [".github/workflows/ci.yml"]',
        ),
    )
    with pytest.raises(ConfigurationError, match="require a workflow check provider"):
        load_repository_check_plan(unused)


def test_dollar_and_percent_workflow_paths_are_preserved_as_literals(tmp_path: Path) -> None:
    path = write_plan(
        tmp_path,
        plan_document(
            'checks = ["workflow"]',
            'workflow-paths = ["$WORKFLOWS/%literal%/ci.yml"]',
        ),
    )

    plan = load_repository_check_plan(path)

    assert plan.workflow_paths == (PurePosixPath("$WORKFLOWS/%literal%/ci.yml"),)


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ('workflow-security-profile = "unknown"', "unknown workflow security profile"),
        (
            'workflow-security-profile = "custom"',
            "custom workflow security profile requires explicit rules",
        ),
        ("workflow-security-profile = true", "must be a string"),
        ("workflow-security-rules = []", "must not be empty"),
        ('workflow-security-rules = ["unknown"]', "unknown workflow security rule"),
        (
            'workflow-security-rules = ["permissions.explicit", "permissions.explicit"]',
            "must not be repeated",
        ),
        (
            "workflow-security-rules = ["
            + ", ".join(['"permissions.explicit"'] * (MAX_SECURITY_RULES + 1))
            + "]",
            f"exceeds {MAX_SECURITY_RULES} entries",
        ),
        ("workflow-security-rules = [true]", "array of strings"),
    ],
)
def test_security_selection_is_closed_and_strict(
    tmp_path: Path,
    selection: str,
    message: str,
) -> None:
    path = write_plan(
        tmp_path,
        plan_document('checks = ["workflow-security"]', selection),
    )

    with pytest.raises(ConfigurationError, match=message):
        load_repository_check_plan(path)


def test_security_profile_and_rules_are_mutually_exclusive_even_when_empty(
    tmp_path: Path,
) -> None:
    path = write_plan(
        tmp_path,
        plan_document(
            'checks = ["workflow-security"]',
            'workflow-security-profile = "recommended-v1"',
            "workflow-security-rules = []",
        ),
    )

    with pytest.raises(ConfigurationError, match="choose either"):
        load_repository_check_plan(path)


@pytest.mark.parametrize(
    "selection",
    [
        'workflow-security-profile = "recommended-v1"',
        'workflow-security-rules = ["permissions.explicit"]',
    ],
)
def test_security_selection_requires_its_provider(
    tmp_path: Path,
    selection: str,
) -> None:
    path = write_plan(
        tmp_path,
        plan_document('checks = ["workflow"]', selection),
    )

    with pytest.raises(ConfigurationError, match="require the workflow-security check"):
        load_repository_check_plan(path)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: RepositoryCheckPlan(2, (RepositoryProvider.COMMIT,)),
        lambda: RepositoryCheckPlan(1, ()),
        lambda: RepositoryCheckPlan(
            1,
            (RepositoryProvider.WORKFLOW, RepositoryProvider.COMMIT),
        ),
        lambda: RepositoryCheckPlan(
            1,
            (RepositoryProvider.COMMIT,),
            workflow_paths=(PurePosixPath("ci.yml"),),
        ),
        lambda: RepositoryCheckPlan(
            1,
            (RepositoryProvider.WORKFLOW,),
            workflow_security_profile=WorkflowSecurityProfile.RECOMMENDED_V1,
        ),
        lambda: RepositoryCheckPlan(
            1,
            (RepositoryProvider.WORKFLOW,),
            workflow_paths=(PurePosixPath("CI.yml"), PurePosixPath("ci.yml")),
        ),
        lambda: RepositoryCheckPlan(
            1,
            (RepositoryProvider.WORKFLOW_SECURITY,),
            workflow_security_profile=cast(WorkflowSecurityProfile, "recommended-v1"),
        ),
    ],
)
def test_plan_model_rejects_noncanonical_or_unscoped_state(
    factory: Callable[[], RepositoryCheckPlan],
) -> None:
    with pytest.raises(ValueError, match="repository check plan"):
        factory()
