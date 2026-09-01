"""Tests for bounded workflow discovery and loading."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from yaga.errors import InputError
from yaga.workflows import inputs
from yaga.workflows.inputs import WorkflowInput, load_workflow_inputs


def write_workflow(path: Path, content: bytes = b"jobs: {}\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_default_discovery_is_sorted_bounded_and_direct_child_only(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "z.yaml", b"jobs: {z: {}}\n")
    write_workflow(workflows / "a.yml", b"jobs: {a: {}}\n")
    write_workflow(workflows / "nested" / "ignored.yml", b"ignored\n")
    (workflows / "notes.txt").write_text("ignored", encoding="utf-8")

    loaded = load_workflow_inputs(tmp_path)

    assert [item.relative_path for item in loaded] == [
        ".github/workflows/a.yml",
        ".github/workflows/z.yaml",
    ]
    assert [item.path for item in loaded] == [
        (workflows / "a.yml").resolve(),
        (workflows / "z.yaml").resolve(),
    ]
    assert [item.content for item in loaded] == [b"jobs: {a: {}}\n", b"jobs: {z: {}}\n"]


def test_explicit_files_and_directories_are_deduplicated(tmp_path: Path) -> None:
    examples = tmp_path / "examples"
    first = examples / "first.yml"
    second = examples / "second.yaml"
    write_workflow(first)
    write_workflow(second)

    loaded = load_workflow_inputs(
        tmp_path,
        [Path("examples"), first, Path("examples/first.yml")],
    )

    assert [item.relative_path for item in loaded] == [
        "examples/first.yml",
        "examples/second.yaml",
    ]


def test_workflow_input_is_immutable(tmp_path: Path) -> None:
    workflow = WorkflowInput(
        path=tmp_path / "ci.yml",
        relative_path="ci.yml",
        content=b"jobs: {}\n",
    )

    with pytest.raises(FrozenInstanceError):
        workflow.__setattr__("content", b"changed")


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        (Path("missing"), "does not exist"),
        (Path("empty"), "contains no"),
        (Path("workflow.txt"), "extension"),
    ],
)
def test_invalid_or_empty_selections_fail_loudly(
    tmp_path: Path,
    selection: Path,
    message: str,
) -> None:
    target = tmp_path / selection
    if selection.name == "empty":
        target.mkdir()
    elif selection.name == "workflow.txt":
        target.write_text("name: ignored\n", encoding="utf-8")

    with pytest.raises(InputError, match=message):
        load_workflow_inputs(tmp_path, [selection])


def test_selection_cannot_escape_the_repository(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.yml"
    write_workflow(outside)

    with pytest.raises(InputError, match="inside the repository"):
        load_workflow_inputs(tmp_path, [outside])


def test_selection_errors_bound_and_sanitize_displayed_paths(tmp_path: Path) -> None:
    selection = Path("missing\n::error::" + "x" * 500)

    with pytest.raises(InputError) as caught:
        load_workflow_inputs(tmp_path, [selection])

    message = str(caught.value)
    assert "\n" not in message
    assert len(message) < 240


def test_read_errors_bound_and_sanitize_displayed_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = tmp_path / "unsafe\u202e.yml"
    write_workflow(workflow)

    def fail_read(path: Path, *, maximum: int) -> bytes:
        raise OSError(f"raw failure for {path} at {maximum}")

    monkeypatch.setattr(inputs, "read_file_prefix", fail_read)

    with pytest.raises(InputError) as caught:
        load_workflow_inputs(tmp_path, [workflow])

    message = str(caught.value)
    assert "\u202e" not in message
    assert "raw failure" not in message
    assert len(message) < 240


def test_selection_and_discovered_file_counts_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "one.yml")
    write_workflow(workflows / "two.yml")
    monkeypatch.setattr(inputs, "MAX_WORKFLOW_FILES", 1)

    with pytest.raises(InputError, match="path limit"):
        load_workflow_inputs(tmp_path, [Path("one.yml"), Path("two.yml")])
    with pytest.raises(InputError, match="file limit"):
        load_workflow_inputs(tmp_path)


def test_per_file_and_aggregate_byte_limits_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    write_workflow(workflows / "one.yml", b"12345")
    write_workflow(workflows / "two.yml", b"67890")

    monkeypatch.setattr(inputs, "MAX_TOTAL_BYTES", 9)
    with pytest.raises(InputError, match="byte total"):
        load_workflow_inputs(tmp_path)

    monkeypatch.setattr(inputs, "MAX_TOTAL_BYTES", 100)
    monkeypatch.setattr(inputs, "MAX_WORKFLOW_BYTES", 4)
    with pytest.raises(InputError, match="hard 4-byte limit"):
        load_workflow_inputs(tmp_path, [Path(".github/workflows/one.yml")])


def test_directory_entry_enumeration_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for index in range(3):
        (workflows / f"note-{index}.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(inputs, "MAX_DIRECTORY_ENTRIES", 2)

    with pytest.raises(InputError, match="entry limit"):
        load_workflow_inputs(tmp_path)


def test_public_input_limits_are_fixed() -> None:
    assert inputs.MAX_WORKFLOW_FILES == 128
    assert inputs.MAX_DIRECTORY_ENTRIES == 1024
    assert inputs.MAX_WORKFLOW_BYTES == 1024 * 1024
    assert inputs.MAX_TOTAL_BYTES == 8 * 1024 * 1024
    assert inputs.MAX_WORKFLOW_PATH_BYTES == 4096
    assert inputs.MAX_WORKFLOW_PATH_COMPONENTS == 64


@pytest.mark.parametrize(
    "relative_path",
    [
        "x" * 4097,
        "/".join("x" for _ in range(65)),
        "../outside.yml",
        "unsafe\ud800.yml",
    ],
)
def test_repository_relative_workflow_paths_are_bounded(relative_path: str) -> None:
    with pytest.raises(InputError, match="workflow path"):
        inputs.validate_workflow_relative_path(relative_path)


def test_repository_relative_workflow_path_accepts_the_public_boundaries() -> None:
    inputs.validate_workflow_relative_path("x" * 4096)
    inputs.validate_workflow_relative_path("/".join("x" for _ in range(64)))
