"""Tests for standalone, preloaded, and parsed workflow-security services."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from yaga.errors import InputError
from yaga.workflows import security
from yaga.workflows.inputs import WorkflowInput
from yaga.workflows.models import WorkflowDiagnostic
from yaga.workflows.parser import ParsedWorkflowInput, parse_workflow_inputs
from yaga.workflows.security import (
    check_parsed_workflow_security_inputs,
    check_workflow_security,
    check_workflow_security_inputs,
)
from yaga.workflows.security_models import WorkflowSecurityProfile, WorkflowSecurityRule


def _workflow(
    repository: Path,
    relative_path: str,
    content: bytes = b"permissions: {}\njobs: {}\n",
) -> WorkflowInput:
    return WorkflowInput(
        path=repository.joinpath(*PurePosixPath(relative_path).parts),
        relative_path=relative_path,
        content=content,
    )


def test_standalone_security_loads_the_requested_selection_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (_workflow(repository, ".github/workflows/ci.yml"),)
    paths = (Path("chosen.yml"),)
    calls: list[tuple[Path, tuple[Path, ...]]] = []

    def load(received_repository: Path, received_paths: tuple[Path, ...]):
        calls.append((received_repository, received_paths))
        return selected

    monkeypatch.setattr(security, "load_workflow_inputs", load)

    report = check_workflow_security(repository, paths)

    assert calls == [(repository, paths)]
    assert report.profile is WorkflowSecurityProfile.RECOMMENDED_V1
    assert report.valid is True
    assert [result.path for result in report.results] == [".github/workflows/ci.yml"]


def test_preloaded_security_uses_exact_content_without_rediscovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (
        _workflow(
            repository,
            ".github/workflows/ci.yml",
            b"jobs: {}\n",
        ),
    )
    monkeypatch.setattr(
        security,
        "load_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("preloaded checking must not rediscover workflows"),
    )

    report = check_workflow_security_inputs(selected)

    assert report.valid is False
    assert [item.code for item in report.results[0].diagnostics] == [
        "security.permissions.explicit"
    ]


def test_parsed_security_does_not_compose_yaml_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    parsed = parse_workflow_inputs((_workflow(repository, ".github/workflows/ci.yml"),))
    monkeypatch.setattr(
        security,
        "parse_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("parsed checking must not compose YAML again"),
    )

    report = check_parsed_workflow_security_inputs(
        parsed,
        rules=("permissions.explicit",),
    )

    assert report.profile is WorkflowSecurityProfile.CUSTOM
    assert report.rules == (WorkflowSecurityRule.PERMISSIONS_EXPLICIT,)
    assert report.valid is True


def test_selection_errors_precede_standalone_path_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        security,
        "load_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("invalid rules must fail before discovery"),
    )

    with pytest.raises(InputError, match="unknown workflow security rule"):
        check_workflow_security(tmp_path, rules=("unknown",))


def test_security_diagnostics_are_hard_bounded_across_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    parsed = parse_workflow_inputs(
        (
            _workflow(repository, ".github/workflows/one.yml"),
            _workflow(repository, ".github/workflows/two.yml"),
        )
    )
    diagnostics = tuple(
        WorkflowDiagnostic(
            code="security.permissions.explicit",
            message="workflow must declare top-level permissions",
            line=index,
            column=1,
        )
        for index in (1, 2)
    )
    monkeypatch.setattr(
        security,
        "evaluate_workflow_security",
        lambda facts, rules: diagnostics,
    )
    monkeypatch.setattr(security, "MAX_TOTAL_SECURITY_DIAGNOSTICS", 3)

    with pytest.raises(InputError, match="hard 3-diagnostic total limit"):
        check_parsed_workflow_security_inputs(parsed)


def test_parsed_security_rejects_empty_and_invalid_items(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="nonempty tuple"):
        check_parsed_workflow_security_inputs(())

    with pytest.raises(InputError, match="invalid item"):
        check_parsed_workflow_security_inputs(cast(Any, (object(),)))

    repository = tmp_path / "repository"
    repository.mkdir()
    invalid = ParsedWorkflowInput(
        input=_workflow(repository, ".github/workflows/ci.yml"),
        parsed=cast(Any, object()),
    )
    with pytest.raises(InputError, match="invalid parse bundle"):
        check_parsed_workflow_security_inputs((invalid,))


def test_parsed_security_preserves_preloaded_repository_validation(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_parsed = parse_workflow_inputs((_workflow(first, "one.yml"),))[0]
    second_parsed = parse_workflow_inputs((_workflow(second, "two.yml"),))[0]

    with pytest.raises(InputError, match="one repository"):
        check_parsed_workflow_security_inputs((first_parsed, second_parsed))


def test_parsed_security_rechecks_the_cross_file_node_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    parsed = parse_workflow_inputs((_workflow(repository, ".github/workflows/ci.yml"),))
    monkeypatch.setattr(security, "MAX_TOTAL_PARSED_NODES", 1)

    with pytest.raises(InputError, match="hard 1-node total limit"):
        check_parsed_workflow_security_inputs(parsed)
