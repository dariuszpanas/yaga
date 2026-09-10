"""Tests for workflow providers consuming one shared preloaded input tuple."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from yaga.errors import InputError
from yaga.workflows import checker, lint
from yaga.workflows import inputs as workflow_inputs
from yaga.workflows.inputs import WorkflowInput
from yaga.workflows.models import WorkflowLintReport, WorkflowReport


def _workflow(
    repository: Path,
    relative_path: str,
    content: bytes = b"jobs: {}\n",
) -> WorkflowInput:
    return WorkflowInput(
        path=repository.joinpath(*PurePosixPath(relative_path).parts),
        relative_path=relative_path,
        content=content,
    )


def test_check_workflow_inputs_uses_preloaded_content_without_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (_workflow(repository, ".github/workflows/ci.yml"),)
    monkeypatch.setattr(
        checker,
        "load_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("preloaded checking must not rediscover workflows"),
    )

    report = checker.check_workflow_inputs(selected)

    assert report.valid is True
    assert [result.path for result in report.results] == [".github/workflows/ci.yml"]


def test_check_workflows_delegates_its_loaded_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (_workflow(repository, ".github/workflows/ci.yml"),)
    expected = WorkflowReport(results=())
    selections = (Path("chosen.yml"),)
    monkeypatch.setattr(
        checker,
        "load_workflow_inputs",
        lambda received_repository, received_selections: (
            selected
            if (received_repository, received_selections) == (repository, selections)
            else pytest.fail("standalone checking changed its discovery arguments")
        ),
    )
    monkeypatch.setattr(
        checker,
        "check_workflow_inputs",
        lambda received: (
            expected
            if received is selected
            else pytest.fail("standalone checking did not delegate the loaded tuple")
        ),
    )

    assert checker.check_workflows(repository, selections) is expected


def test_lint_workflow_inputs_uses_preloaded_content_without_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (_workflow(repository, ".github/workflows/ci.yml"),)
    expected = WorkflowLintReport(results=())
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        lint,
        "load_workflow_inputs",
        lambda *args, **kwargs: pytest.fail("preloaded linting must not rediscover workflows"),
    )
    monkeypatch.setattr(lint, "_find_docker", lambda received: "C:/tools/docker.exe")

    def lint_in_workspace(
        received_repository: Path,
        received_inputs: tuple[WorkflowInput, ...],
        docker: str,
        runtime_directory: Path,
    ) -> WorkflowLintReport:
        captured.update(
            repository=received_repository,
            inputs=received_inputs,
            docker=docker,
            runtime_directory=runtime_directory,
        )
        return expected

    monkeypatch.setattr(lint, "_lint_in_temporary_workspace", lint_in_workspace)

    assert lint.lint_workflow_inputs(repository, selected) is expected
    assert captured["repository"] == repository.resolve()
    assert captured["inputs"] is selected
    assert captured["docker"] == "C:/tools/docker.exe"
    assert isinstance(captured["runtime_directory"], Path)


def test_lint_workflows_delegates_its_loaded_tuple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (_workflow(repository, ".github/workflows/ci.yml"),)
    expected = WorkflowLintReport(results=())
    selections = (Path("chosen.yml"),)
    monkeypatch.setattr(
        lint,
        "load_workflow_inputs",
        lambda received_repository, received_selections: (
            selected
            if (received_repository, received_selections) == (repository, selections)
            else pytest.fail("standalone linting changed its discovery arguments")
        ),
    )
    monkeypatch.setattr(
        lint,
        "lint_workflow_inputs",
        lambda received_repository, received_inputs: (
            expected
            if (received_repository, received_inputs) == (repository, selected)
            else pytest.fail("standalone linting did not delegate the loaded tuple")
        ),
    )

    assert lint.lint_workflows(repository, selections) is expected


def test_preloaded_providers_reject_empty_inputs(tmp_path: Path) -> None:
    with pytest.raises(InputError, match="nonempty tuple"):
        checker.check_workflow_inputs(())
    with pytest.raises(InputError, match="nonempty tuple"):
        lint.lint_workflow_inputs(tmp_path, ())


def test_preloaded_check_requires_one_repository(tmp_path: Path) -> None:
    first_repository = tmp_path / "first"
    second_repository = tmp_path / "second"
    first_repository.mkdir()
    second_repository.mkdir()
    selected = (
        _workflow(first_repository, ".github/workflows/one.yml"),
        _workflow(second_repository, ".github/workflows/two.yml"),
    )

    with pytest.raises(InputError, match="one repository"):
        checker.check_workflow_inputs(selected)


def test_preloaded_lint_requires_the_provided_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    other_repository = tmp_path / "other"
    repository.mkdir()
    other_repository.mkdir()
    selected = (_workflow(other_repository, ".github/workflows/ci.yml"),)

    with pytest.raises(InputError, match="provided repository"):
        lint.lint_workflow_inputs(repository, selected)


def test_preloaded_inputs_reject_duplicates_path_mismatches_and_oversized_content(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    workflow = _workflow(repository, ".github/workflows/ci.yml")
    with pytest.raises(InputError, match="duplicate path"):
        checker.check_workflow_inputs((workflow, workflow))

    mismatched = WorkflowInput(
        path=repository / ".github" / "workflows" / "other.yml",
        relative_path=".github/workflows/ci.yml",
        content=b"jobs: {}\n",
    )
    with pytest.raises(InputError, match="does not match"):
        checker.check_workflow_inputs((mismatched,))

    oversized = _workflow(
        repository,
        ".github/workflows/large.yml",
        b"x" * (workflow_inputs.MAX_WORKFLOW_BYTES + 1),
    )
    with pytest.raises(InputError, match="byte limit"):
        checker.check_workflow_inputs((oversized,))


def test_preloaded_inputs_preserve_the_shared_file_count_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (
        _workflow(repository, ".github/workflows/one.yml"),
        _workflow(repository, ".github/workflows/two.yml"),
    )
    monkeypatch.setattr(workflow_inputs, "MAX_WORKFLOW_FILES", 1)

    with pytest.raises(InputError, match="file limit"):
        checker.check_workflow_inputs(selected)


@pytest.mark.parametrize(
    "relative_path",
    [
        "foo//bar.yml",
        "foo/./bar.yml",
        "C:/outside.yml",
        "foo\\bar.yml",
        "foo/invalid\x00.yml",
    ],
)
def test_preloaded_providers_reject_noncanonical_or_platform_ambiguous_paths(
    tmp_path: Path,
    relative_path: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (_workflow(repository, relative_path),)

    with pytest.raises(InputError, match="canonical and repository-relative"):
        checker.check_workflow_inputs(selected)
    with pytest.raises(InputError, match="canonical and repository-relative"):
        lint.lint_workflow_inputs(repository, selected)


def test_preloaded_providers_bound_path_resolution_failures(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (
        WorkflowInput(
            path=Path(f"{repository}\x00"),
            relative_path=".github/workflows/ci.yml",
            content=b"jobs: {}\n",
        ),
    )

    with pytest.raises(InputError, match="cannot be resolved safely") as checker_error:
        checker.check_workflow_inputs(selected)
    assert str(checker_error.value) == "preloaded workflow input path cannot be resolved safely"
    with pytest.raises(InputError, match="cannot be resolved safely") as lint_error:
        lint.lint_workflow_inputs(repository, selected)
    assert str(lint_error.value) == "preloaded workflow input path cannot be resolved safely"


def test_preloaded_providers_reject_unresolved_paths_and_invalid_item_shapes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    unresolved = WorkflowInput(
        path=Path(".github/workflows/ci.yml"),
        relative_path=".github/workflows/ci.yml",
        content=b"jobs: {}\n",
    )
    with pytest.raises(InputError, match="absolute and resolved"):
        checker.check_workflow_inputs((unresolved,))

    with pytest.raises(InputError, match="invalid item"):
        checker.check_workflow_inputs(cast(Any, (object(),)))
    with pytest.raises(InputError, match="invalid path"):
        checker.check_workflow_inputs(
            (
                WorkflowInput(
                    path=cast(Any, "not-a-path"),
                    relative_path=".github/workflows/ci.yml",
                    content=b"jobs: {}\n",
                ),
            )
        )
    with pytest.raises(InputError, match="content must be bytes"):
        checker.check_workflow_inputs(
            (
                WorkflowInput(
                    path=repository / ".github" / "workflows" / "ci.yml",
                    relative_path=".github/workflows/ci.yml",
                    content=cast(Any, "jobs: {}"),
                ),
            )
        )
    with pytest.raises(InputError, match="nonempty tuple"):
        checker.check_workflow_inputs(cast(Any, []))


def test_preloaded_inputs_reject_invalid_extensions_and_total_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(InputError, match="extension"):
        checker.check_workflow_inputs((_workflow(repository, "workflow.txt"),))

    selected = (
        _workflow(repository, ".github/workflows/one.yml", b"12"),
        _workflow(repository, ".github/workflows/two.yml", b"34"),
    )
    monkeypatch.setattr(workflow_inputs, "MAX_TOTAL_BYTES", 3)
    with pytest.raises(InputError, match="byte total limit"):
        checker.check_workflow_inputs(selected)


def test_preloaded_lint_bounds_invalid_repository_resolution(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (_workflow(repository, ".github/workflows/ci.yml"),)

    with pytest.raises(InputError, match="repository path cannot be resolved") as raised:
        lint.lint_workflow_inputs(Path("invalid\x00repository"), selected)
    assert str(raised.value) == "workflow repository path cannot be resolved safely"


@pytest.mark.skipif(
    Path("A") != Path("a"),
    reason="case-variant paths are distinct on this platform",
)
def test_preloaded_inputs_reject_case_variant_duplicate_resolved_paths(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    selected = (
        _workflow(repository, ".github/workflows/ci.yml"),
        _workflow(repository, ".GITHUB/WORKFLOWS/CI.YML"),
    )

    with pytest.raises(InputError, match="duplicate resolved path"):
        checker.check_workflow_inputs(selected)
