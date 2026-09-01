"""Adversarial tests for actionlint snapshots and Docker lifecycle isolation."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import InputError
from yaga.workflows import actionlint_runtime as runtime
from yaga.workflows import actionlint_snapshot as snapshot
from yaga.workflows import lint
from yaga.workflows.inputs import WorkflowInput
from yaga.workflows.models import (
    ActionManifestDependency,
    ReferenceContext,
    WorkflowReference,
)
from yaga.workflows.yaml import parse_workflow


def _write(repository: Path, relative_path: str, content: bytes | str) -> Path:
    path = repository.joinpath(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def _input(repository: Path, relative_path: str) -> WorkflowInput:
    path = repository.joinpath(*relative_path.split("/"))
    return WorkflowInput(
        path=path,
        relative_path=relative_path,
        content=path.read_bytes(),
    )


def _process_result(
    returncode: int = 0,
    *,
    stdout: bytes = b"[]",
    stderr: bytes = b"",
    timed_out: bool = False,
    stdout_overflow: bool = False,
    stderr_overflow: bool = False,
) -> lint._ProcessResult:
    return lint._ProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_overflow=stdout_overflow,
        stderr_overflow=stderr_overflow,
    )


def test_snapshot_closes_reusable_action_config_and_runtime_dependencies(tmp_path: Path) -> None:
    caller = _write(
        tmp_path,
        ".github/workflows/caller.yml",
        """name: Caller
on: push
env:
  ANCHOR: &reuse '$/.github/workflows/reuse.yml'
  UNRELATED: '$/.github/workflows/reuse.yml'
jobs:
  call:
    uses: *reuse
""",
    )
    _write(
        tmp_path,
        ".github/workflows/reuse.yml",
        """name: Reuse
on: workflow_call
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: $/actions/local
""",
    )
    _write(
        tmp_path,
        "actions/local/action.yml",
        """name: Local
description: Local action
runs:
  using: node20
  main: dist/main.js
  pre: scripts/pre.js
  post: ../shared/post.js
""",
    )
    _write(
        tmp_path,
        "actions/local/dist/main.js",
        b"x" * (lint.MAX_WORKFLOW_BYTES + 1),
    )
    _write(tmp_path, "actions/local/scripts/pre.js", b"pre")
    _write(tmp_path, "actions/shared/post.js", b"post")
    _write(tmp_path, ".github/actionlint.yaml", "config-variables: [ALLOWED]\n")

    snapshot = lint._build_actionlint_snapshot(
        tmp_path.resolve(),
        (
            WorkflowInput(
                path=caller.resolve(),
                relative_path=".github/workflows/caller.yml",
                content=caller.read_bytes(),
            ),
        ),
    )

    assert tuple(snapshot) == tuple(sorted(snapshot))
    assert set(snapshot) == {
        ".github/actionlint.yaml",
        ".github/workflows/caller.yml",
        ".github/workflows/reuse.yml",
        "actions/local/action.yml",
        "actions/local/dist/main.js",
        "actions/local/scripts/pre.js",
        "actions/shared/post.js",
    }
    normalized_caller = snapshot[".github/workflows/caller.yml"]
    assert b"&reuse './.github/workflows/reuse.yml'" in normalized_caller
    assert normalized_caller.count(b"$/.github/workflows/reuse.yml") == 1
    assert b"uses: ./actions/local" in snapshot[".github/workflows/reuse.yml"]
    assert snapshot["actions/local/dist/main.js"] == b""
    assert snapshot["actions/local/scripts/pre.js"] == b""
    assert snapshot["actions/shared/post.js"] == b""


def test_snapshot_uses_action_yaml_precedence_and_ignores_shadow_manifest(tmp_path: Path) -> None:
    workflow = _write(
        tmp_path,
        ".github/workflows/ci.yml",
        """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: ./action
""",
    )
    _write(
        tmp_path,
        "action/action.yaml",
        """name: Chosen
description: Chosen manifest
runs:
  using: composite
  steps: []
""",
    )
    _write(
        tmp_path,
        "action/action.yml",
        """name: Ignored
description: Ignored manifest
runs:
  using: node20
  main: ../../outside.js
""",
    )

    snapshot = lint._build_actionlint_snapshot(
        tmp_path.resolve(),
        (_input(tmp_path, ".github/workflows/ci.yml"),),
    )

    assert workflow.exists()
    assert "action/action.yaml" in snapshot
    assert "action/action.yml" not in snapshot
    assert "outside.js" not in snapshot


def test_snapshot_collects_native_local_dependencies_with_spaces(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        """name: CI
on: push
jobs:
  call:
    uses: "$/.github/workflows/called workflow.yml"
  local:
    runs-on: ubuntu-latest
    steps:
      - uses: "./actions/with space"
""",
    )
    _write(
        tmp_path,
        ".github/workflows/called workflow.yml",
        "on: workflow_call\njobs: {}\n",
    )
    _write(
        tmp_path,
        "actions/with space/action.yml",
        "name: Spaced action\nruns: {using: node20, main: index.js}\n",
    )
    _write(tmp_path, "actions/with space/index.js", b"")

    staged = lint._build_actionlint_snapshot(
        tmp_path.resolve(),
        (_input(tmp_path, ".github/workflows/ci.yml"),),
    )

    assert ".github/workflows/called workflow.yml" in staged
    assert "actions/with space/action.yml" in staged
    assert staged["actions/with space/index.js"] == b""
    assert b'uses: "./.github/workflows/called workflow.yml"' in staged[".github/workflows/ci.yml"]


@pytest.mark.parametrize(
    ("value", "context", "expected"),
    [
        ("./actions/with space", ReferenceContext.STEP, "actions/with space"),
        ("$/actions/with space", ReferenceContext.STEP, "actions/with space"),
        (
            "./.github/workflows/called workflow.yml",
            ReferenceContext.JOB,
            ".github/workflows/called workflow.yml",
        ),
        (
            "$/.github/workflows/called workflow.yml",
            ReferenceContext.JOB,
            ".github/workflows/called workflow.yml",
        ),
        ("./actions/../with space", ReferenceContext.STEP, "with space"),
        ("./actions//./local", ReferenceContext.STEP, "actions/local"),
        ("./actions/hash#query?value%20", ReferenceContext.STEP, "actions/hash#query?value%20"),
        ("./actions/@local", ReferenceContext.STEP, "actions/@local"),
        ("./../outside", ReferenceContext.STEP, None),
        ("$/inside/../../outside", ReferenceContext.JOB, None),
        ("./actions/${{ matrix.name }}", ReferenceContext.STEP, None),
        ("./actions\\local", ReferenceContext.STEP, None),
        ("./C:/outside", ReferenceContext.STEP, None),
        ("./@workflow.yml", ReferenceContext.JOB, None),
    ],
)
def test_lint_local_reference_resolution_is_bounded_but_not_policy_coupled(
    value: str,
    context: ReferenceContext,
    expected: str | None,
) -> None:
    reference = WorkflowReference(value=value, context=context, line=1, column=1)

    assert snapshot._resolve_local_reference_path(reference) == expected


@pytest.mark.parametrize(
    "value",
    [
        f"./{'x' * (lint.MAX_ACTIONLINT_PATH_BYTES + 1)}",
        "./" + "/".join("x" for _ in range(lint.MAX_ACTIONLINT_PATH_COMPONENTS + 1)),
    ],
)
def test_lint_local_reference_resolution_enforces_support_limits(value: str) -> None:
    reference = WorkflowReference(
        value=value,
        context=ReferenceContext.STEP,
        line=1,
        column=1,
    )

    with pytest.raises(InputError, match="bounded support limit"):
        snapshot._resolve_local_reference_path(reference)


def test_snapshot_models_runtime_directories_without_file_prefix_collisions(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        """jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: ./action
""",
    )
    _write(
        tmp_path,
        "action/action.yml",
        """name: Local
description: Local
runs:
  using: node20
  main: dist
  pre: dist/pre.js
""",
    )
    _write(tmp_path, "action/dist/pre.js", b"pre")

    staged = lint._build_actionlint_snapshot(
        tmp_path.resolve(),
        (_input(tmp_path, ".github/workflows/ci.yml"),),
    )

    assert "action/dist" not in staged
    assert "action/dist" in staged.directories
    assert "action/dist/.yaga-actionlint-directory" not in staged
    assert staged["action/dist/pre.js"] == b""
    archive_bytes = lint._build_actionlint_archive(staged)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
        assert archive.getmember("action/dist").isdir()


def test_directory_presence_never_fabricates_a_declared_runtime_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs: {test: {runs-on: ubuntu-latest, steps: [{uses: ./action}]}}\n",
    )
    _write(
        tmp_path,
        "action/action.yml",
        """name: Local
description: Local
runs:
  using: node20
  main: dist
  pre: dist/.yaga-actionlint-directory
""",
    )
    (tmp_path / "action" / "dist").mkdir()

    staged = lint._build_actionlint_snapshot(
        tmp_path.resolve(),
        (_input(tmp_path, ".github/workflows/ci.yml"),),
    )

    assert "action/dist" in staged.directories
    assert "action/dist/.yaga-actionlint-directory" not in staged


def test_snapshot_preserves_non_utf8_action_metadata_for_actionlint(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "jobs: {test: {runs-on: ubuntu-latest, steps: [{uses: ./action}]}}\n",
    )
    manifest = _write(tmp_path, "action/action.yml", b"name: \xff\n")

    staged = lint._build_actionlint_snapshot(
        tmp_path.resolve(),
        (_input(tmp_path, ".github/workflows/ci.yml"),),
    )

    assert staged["action/action.yml"] == manifest.read_bytes()


def test_self_reference_rewrite_changes_only_exact_scalar_source(tmp_path: Path) -> None:
    raw = b"""name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: \"$/actions/local\"
      - run: echo '$/actions/local'
        env: {VALUE: '$/actions/local'}
      # $/actions/local
"""
    references = parse_workflow(raw, label="ci.yml").references

    normalized = lint._normalize_self_references(raw, references)

    assert b'uses: "./actions/local"' in normalized
    assert normalized.count(b"$/actions/local") == 3
    assert len(normalized) == len(raw)


def test_self_reference_rewrite_rejects_nonliteral_scalar_encoding() -> None:
    raw = b"""name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: "\\u0024/actions/local"
"""
    references = parse_workflow(raw, label="ci.yml").references

    with pytest.raises(InputError, match="cannot be normalized safely"):
        lint._normalize_self_references(raw, references)


def test_snapshot_preserves_malformed_workflow_for_actionlint(tmp_path: Path) -> None:
    workflow = _write(tmp_path, ".github/workflows/bad.yml", b"jobs: [\n")

    snapshot = lint._build_actionlint_snapshot(
        tmp_path.resolve(),
        (_input(tmp_path, ".github/workflows/bad.yml"),),
    )

    assert snapshot[".github/workflows/bad.yml"] == workflow.read_bytes()


def test_workflow_parser_resource_limits_remain_operational_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = "".join(f"  KEY_{index}: value\n" for index in range(10_050))
    workflow = _write(
        tmp_path,
        ".github/workflows/large.yml",
        f"env:\n{environment}jobs: {{}}\n",
    )
    selected = (_input(tmp_path, ".github/workflows/large.yml"),)
    monkeypatch.setattr(lint, "load_workflow_inputs", lambda repository, selections: selected)
    monkeypatch.setattr(lint, "_find_docker", lambda repository: "docker")

    with pytest.raises(InputError, match="node limit"):
        lint.lint_workflows(tmp_path)

    assert workflow.stat().st_size < lint.MAX_WORKFLOW_BYTES


def test_snapshot_rejects_both_native_config_names_even_when_preselected(tmp_path: Path) -> None:
    yaml_config = _write(tmp_path, ".github/actionlint.yaml", "paths: {}\n")
    yml_config = _write(tmp_path, ".github/actionlint.yml", "paths: {}\n")

    with pytest.raises(InputError, match="both supported native actionlint config"):
        lint._build_actionlint_snapshot(
            tmp_path.resolve(),
            (
                WorkflowInput(
                    path=yaml_config.resolve(),
                    relative_path=".github/actionlint.yaml",
                    content=yaml_config.read_bytes(),
                ),
                WorkflowInput(
                    path=yml_config.resolve(),
                    relative_path=".github/actionlint.yml",
                    content=yml_config.read_bytes(),
                ),
            ),
        )


def test_snapshot_support_files_obey_file_byte_node_and_path_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(
        tmp_path,
        ".github/workflows/ci.yml",
        """name: CI
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: ./action
""",
    )
    _write(
        tmp_path,
        "action/action.yml",
        "name: A\ndescription: A\nruns: {using: node20, main: index.js}\n",
    )
    _write(tmp_path, "action/index.js", b"runtime")
    selected = (_input(tmp_path, ".github/workflows/ci.yml"),)

    monkeypatch.setattr(snapshot, "MAX_ACTIONLINT_SNAPSHOT_FILES", 1)
    with pytest.raises(InputError, match="file limit"):
        lint._build_actionlint_snapshot(tmp_path.resolve(), selected)

    monkeypatch.setattr(snapshot, "MAX_ACTIONLINT_SNAPSHOT_FILES", 256)
    monkeypatch.setattr(snapshot, "MAX_ACTIONLINT_SNAPSHOT_BYTES", len(selected[0].content) + 1)
    with pytest.raises(InputError, match="byte limit"):
        lint._build_actionlint_snapshot(tmp_path.resolve(), selected)

    monkeypatch.setattr(snapshot, "MAX_ACTIONLINT_SNAPSHOT_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(snapshot, "MAX_ACTIONLINT_SNAPSHOT_NODES", 1)
    with pytest.raises(InputError, match="YAML-node limit"):
        lint._build_actionlint_snapshot(tmp_path.resolve(), selected)

    with pytest.raises(InputError, match="repository-relative"):
        lint._validate_snapshot_relative_path("x" * (lint.MAX_ACTIONLINT_PATH_BYTES + 1))
    with pytest.raises(InputError, match="repository-relative"):
        lint._validate_snapshot_relative_path(
            "/".join("x" for _ in range(lint.MAX_ACTIONLINT_PATH_COMPONENTS + 1))
        )


def test_action_runtime_path_is_bounded_to_repository_and_remote_images_are_skipped() -> None:
    assert (
        lint._action_runtime_path(
            "actions/local/action.yml",
            ActionManifestDependency(field="main", value="../shared/main.js"),
        )
        == "actions/shared/main.js"
    )
    assert (
        lint._action_runtime_path(
            "actions/local/action.yml",
            ActionManifestDependency(field="main", value="../../../outside.js"),
        )
        is None
    )
    assert (
        lint._action_runtime_path(
            "actions/local/action.yml",
            ActionManifestDependency(field="main", value="C:\\outside.js"),
        )
        is None
    )
    assert (
        lint._action_runtime_path(
            "actions/local/action.yml",
            ActionManifestDependency(field="image", value="ghcr.io/example/action:latest"),
        )
        is None
    )


def test_snapshot_rejects_support_symlink_that_escapes_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = _write(tmp_path, "outside.yml", "paths: {}\n")
    link = repository / ".github" / "actionlint.yaml"
    link.parent.mkdir()
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    workflow = _write(repository, ".github/workflows/ci.yml", "jobs: {}\n")

    with pytest.raises(InputError, match="cannot be read safely"):
        lint._build_actionlint_snapshot(
            repository.resolve(),
            (
                WorkflowInput(
                    path=workflow.resolve(),
                    relative_path=".github/workflows/ci.yml",
                    content=workflow.read_bytes(),
                ),
            ),
        )


def test_actionlint_archive_is_deterministic_pax_and_regular_file_only() -> None:
    long_name = f"nested/{'a' * 120}-é.yml"
    files = {long_name: b"workflow", ".github/actionlint.yaml": b"paths: {}\n"}

    first = lint._build_actionlint_archive(files)
    second = lint._build_actionlint_archive(dict(reversed(tuple(files.items()))))

    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:*") as archive:
        members = archive.getmembers()
        assert {member.name for member in members if member.isfile()} == set(files)
        assert {member.name for member in members if member.isdir()} >= {
            ".git",
            ".github",
            ".github/workflows",
            "nested",
        }
        assert all(member.isfile() or member.isdir() for member in members)
        assert all(member.uid == 0 and member.gid == 0 and member.mtime == 0 for member in members)
        assert all(member.mode == (0o444 if member.isfile() else 0o555) for member in members)
        for name, content in files.items():
            extracted = archive.extractfile(name)
            assert extracted is not None
            assert extracted.read() == content


def test_actionlint_archive_obeys_its_encoded_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(snapshot, "MAX_ACTIONLINT_ARCHIVE_BYTES", 1)
    with pytest.raises(InputError, match="archive exceeds"):
        lint._build_actionlint_archive({"workflow.yml": b"jobs: {}\n"})


def test_actionlint_archive_obeys_its_entry_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot, "MAX_ACTIONLINT_ARCHIVE_ENTRIES", 4)
    with pytest.raises(InputError, match="entry limit"):
        lint._build_actionlint_archive({"one/two/workflow.yml": b"jobs: {}\n"})


@pytest.mark.parametrize(
    "files",
    [
        {"dist": b"file", "dist/pre.js": b"child"},
        {".github": b"file"},
    ],
)
def test_actionlint_archive_rejects_file_directory_collisions(
    files: dict[str, bytes],
) -> None:
    with pytest.raises(InputError, match="file-directory collision"):
        lint._build_actionlint_archive(files)


def test_archive_entry_bound_can_represent_every_public_snapshot_path() -> None:
    maximum_entries = lint.MAX_ACTIONLINT_SNAPSHOT_FILES * lint.MAX_ACTIONLINT_PATH_COMPONENTS + 3
    assert lint.MAX_ACTIONLINT_ARCHIVE_ENTRIES >= maximum_entries


def test_docker_discovery_ignores_relative_and_repository_executables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    trusted = tmp_path / "trusted"
    repository.mkdir()
    trusted.mkdir()
    executable_name = "docker.exe" if os.name == "nt" else "docker"
    repository_executable = _write(repository, executable_name, b"repository")
    trusted_executable = _write(trusted, executable_name, b"trusted")
    if os.name != "nt":
        repository_executable.chmod(0o755)
        trusted_executable.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((".", str(repository.resolve()), str(trusted.resolve()))),
    )

    assert lint._find_docker(repository.resolve()) == str(trusted_executable.resolve())

    monkeypatch.setenv("PATH", os.pathsep.join((".", str(repository.resolve()))))
    assert lint._find_docker(repository.resolve()) is None

    original_expanduser = Path.expanduser

    def expanduser(path: Path) -> Path:
        if str(path) == "malformed-home-entry":
            raise RuntimeError("unknown home")
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(("malformed-home-entry", str(trusted.resolve()))),
    )
    assert lint._find_docker(repository.resolve()) == str(trusted_executable.resolve())


def test_docker_process_environment_excludes_repository_helpers_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    trusted = tmp_path / "trusted"
    repository.mkdir()
    trusted.mkdir()
    executable_name = "docker.exe" if os.name == "nt" else "docker"
    helper_name = (
        "docker-credential-attacker.exe" if os.name == "nt" else "docker-credential-attacker"
    )
    helper = _write(repository, helper_name, b"attacker")
    docker = _write(trusted, executable_name, b"trusted").resolve()
    if os.name != "nt":
        helper.chmod(0o755)
        docker.chmod(0o755)
    docker_config = repository / ".docker"
    docker_config.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join((str(repository), str(trusted))))
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    with pytest.raises(InputError, match="DOCKER_CONFIG"):
        runtime.find_docker(repository.resolve())

    trusted_config = trusted / ".docker"
    trusted_config.mkdir()
    monkeypatch.setenv("DOCKER_CONFIG", str(trusted_config))
    discovered = runtime.find_docker(repository.resolve())
    assert discovered == str(docker)
    assert discovered is not None
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> lint._ProcessResult:
        captured["command"] = command
        captured.update(kwargs)
        return _process_result()

    monkeypatch.setattr(runtime, "run_bounded_process", run)

    runtime.run_docker_control_process(
        [discovered, "version"],
        content=b"",
        cwd=tmp_path,
        timeout=1,
        stdout_limit=10,
        stderr_limit=10,
    )

    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert environment["PATH"] == str(trusted.resolve())
    assert environment["DOCKER_CONFIG"] == str(trusted_config.resolve())
    assert str(repository.resolve()) not in environment["PATH"]


def test_failed_creates_always_request_late_exact_name_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaned: list[tuple[str, str, bool]] = []
    failed = _process_result(returncode=1, stderr=b"failed")
    monkeypatch.setattr(runtime, "run_docker_control_process", lambda *args, **kwargs: failed)
    monkeypatch.setattr(
        runtime,
        "remove_workspace_volume",
        lambda docker, directory, name, token, *, wait_for_late=False: cleaned.append(
            ("volume", name, wait_for_late)
        ),
    )
    monkeypatch.setattr(
        runtime,
        "remove_labeled_containers",
        lambda docker, directory, token, *, container_name=None, wait_for_late=False: (
            cleaned.append(("container", container_name or "", wait_for_late))
        ),
    )

    with pytest.raises(InputError, match="could not create"):
        runtime.create_workspace_volume("docker", tmp_path, "private-volume", "token", timeout=1)
    with pytest.raises(InputError, match="could not create"):
        runtime.create_labeled_container(
            "docker",
            tmp_path,
            "token",
            "private-container",
            ["docker", "create"],
            timeout=1,
        )

    assert cleaned == [
        ("volume", "private-volume", True),
        ("container", "private-container", True),
    ]


def test_cleanup_removes_exact_names_and_names_unconfirmed_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> lint._ProcessResult:
        commands.append(command)
        return _process_result()

    monkeypatch.setattr(runtime, "run_docker_control_process", run)
    monkeypatch.setattr(runtime, "_list_labeled_containers", lambda *args, **kwargs: ())
    runtime.remove_labeled_containers(
        "docker",
        tmp_path,
        "token",
        container_name="private-container",
    )
    assert commands[0] == ["docker", "container", "rm", "--force", "private-container"]

    monkeypatch.setattr(
        runtime,
        "_list_labeled_containers",
        lambda *args, **kwargs: (_ for _ in ()).throw(InputError("inspect failed")),
    )
    with pytest.raises(InputError, match="private-container"):
        runtime.remove_labeled_containers(
            "docker",
            tmp_path,
            "token",
            container_name="private-container",
        )

    monkeypatch.setattr(
        runtime,
        "_list_labeled_volumes",
        lambda *args, **kwargs: (_ for _ in ()).throw(InputError("inspect failed")),
    )
    with pytest.raises(InputError, match="private-volume"):
        runtime.remove_workspace_volume(
            "docker",
            tmp_path,
            "private-volume",
            "token",
        )


def test_minimal_actionlint_diagnostic_without_optional_fields_is_accepted() -> None:
    raw = json.dumps(
        [
            {
                "message": "invalid document",
                "filepath": ".github/workflows/ci.yml",
                "line": 0,
                "column": 0,
                "kind": "syntax-check",
            }
        ]
    ).encode()

    diagnostic = lint._parse_diagnostics(
        raw,
        expected_path=".github/workflows/ci.yml",
    )[0]

    assert diagnostic.code == "actionlint.syntax-check"
    assert (diagnostic.line, diagnostic.column) == (1, 1)


@pytest.mark.parametrize(
    "raw",
    [
        b'[{"message":"one","message":"two","filepath":"ci.yml","line":1,"column":1,"kind":"x"}]',
        b'[{"message":"x","filepath":"ci.yml","line":NaN,"column":1,"kind":"x"}]',
        b'[{"message":"x","filepath":"ci.yml","line":Infinity,"column":1,"kind":"x"}]',
        b'[{"message":"x","filepath":"ci.yml","line":1.5,"column":1,"kind":"x"}]',
        b'[{"message":"x","filepath":"ci.yml","line":1,"column":1,"kind":"x","unknown":1}]',
        b'[{"filepath":"ci.yml","line":1,"column":1,"kind":"x"}]',
        b'[{"message":"x","filepath":"ci.yml","line":1,"column":1,"kind":"x","snippet":1}]',
        b'[{"message":"x","filepath":"ci.yml","line":1,"column":1,"kind":"x","end_column":-1}]',
    ],
)
def test_actionlint_json_shape_numbers_and_duplicates_fail_closed(raw: bytes) -> None:
    with pytest.raises(InputError):
        lint._parse_diagnostics(raw, expected_path="ci.yml")


def test_actionlint_json_rejects_integer_before_runtime_digit_conversion() -> None:
    raw = b'[{"message":"x","filepath":"ci.yml","line":' + b"9" * 5000 + b',"column":1,"kind":"x"}]'

    with pytest.raises(InputError, match="malformed JSON"):
        lint._parse_diagnostics(raw, expected_path="ci.yml")


def test_snapshot_staging_uses_stopped_container_and_tar_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def create(
        docker: str,
        runtime: Path,
        token: str,
        container_name: str,
        command: list[str],
        *,
        timeout: float,
    ) -> str:
        captured.update(
            docker=docker,
            runtime=runtime,
            token=token,
            container_name=container_name,
            create_command=command,
            create_timeout=timeout,
        )
        return "b" * 64

    def run(command: list[str], **kwargs: Any) -> lint._ProcessResult:
        captured["copy_command"] = command
        captured["copy_kwargs"] = kwargs
        return _process_result(stdout=b"")

    monkeypatch.setattr(runtime.secrets, "token_hex", lambda size: "a" * 32)
    monkeypatch.setattr(runtime, "create_labeled_container", create)
    monkeypatch.setattr(runtime, "run_docker_control_process", run)
    monkeypatch.setattr(
        runtime,
        "remove_labeled_containers",
        lambda docker, directory, token, **kwargs: captured.update(removed=token),
    )

    runtime.stage_workspace_snapshot(
        "C:/tools/docker.exe",
        tmp_path,
        "private-volume",
        b"tar-stream",
        timeout=5,
    )

    create_command = captured["create_command"]
    assert create_command[:2] == ["C:/tools/docker.exe", "create"]
    assert "start" not in create_command
    assert "type=volume,source=private-volume,target=/workspace,volume-nocopy" in create_command
    assert captured["copy_command"] == [
        "C:/tools/docker.exe",
        "cp",
        "-",
        f"{'b' * 64}:/workspace",
    ]
    assert captured["copy_kwargs"]["content"] == b"tar-stream"
    assert captured["removed"] == "a" * 32
    assert captured["container_name"] == "yaga-actionlint-stage-" + "a" * 32


def test_uncertain_create_cleans_by_label_even_for_base_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, bool]] = []

    def interrupted(*args: object, **kwargs: object) -> lint._ProcessResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "run_docker_control_process", interrupted)
    monkeypatch.setattr(
        runtime,
        "remove_workspace_volume",
        lambda docker, runtime, name, token, *, wait_for_late=False: captured.append(
            ("volume", wait_for_late)
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        runtime.create_workspace_volume(
            "docker",
            tmp_path,
            "volume",
            "token",
            timeout=1,
        )

    monkeypatch.setattr(
        runtime,
        "remove_labeled_containers",
        lambda docker, directory, token, *, container_name=None, wait_for_late=False: (
            captured.append(("container", wait_for_late))
        ),
    )
    with pytest.raises(KeyboardInterrupt):
        runtime.create_labeled_container(
            "docker",
            tmp_path,
            "token",
            "container-name",
            ["docker", "create"],
            timeout=1,
        )

    assert captured == [("volume", True), ("container", True)]


def test_cleanup_regions_begin_before_each_resource_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _write(tmp_path, ".github/workflows/ci.yml", "jobs: {}\n")
    selected = (
        WorkflowInput(
            path=workflow.resolve(),
            relative_path=".github/workflows/ci.yml",
            content=workflow.read_bytes(),
        ),
    )
    removed: list[str] = []
    monkeypatch.setattr(
        lint,
        "_create_workspace_volume",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        lint,
        "_remove_workspace_volume",
        lambda docker, directory, name, token: removed.append(name),
    )
    with pytest.raises(KeyboardInterrupt):
        lint._lint_in_temporary_workspace(tmp_path.resolve(), selected, "docker", tmp_path)
    assert len(removed) == 1

    container_names: list[str] = []
    monkeypatch.setattr(
        runtime,
        "create_labeled_container",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        runtime,
        "remove_labeled_containers",
        lambda docker, directory, token, *, container_name=None, **kwargs: container_names.append(
            container_name or ""
        ),
    )
    monkeypatch.setattr(runtime.secrets, "token_hex", lambda size: "a" * 32)

    with pytest.raises(KeyboardInterrupt):
        runtime.stage_workspace_snapshot("docker", tmp_path, "volume", b"tar", timeout=1)
    with pytest.raises(KeyboardInterrupt):
        runtime.run_actionlint_container(
            "docker",
            tmp_path,
            "volume",
            "ci.yml",
            content=b"jobs: {}\n",
            config_path=None,
            timeout=1,
        )

    assert container_names == [
        "yaga-actionlint-stage-" + "a" * 32,
        "yaga-actionlint-lint-" + "a" * 32,
    ]


@pytest.mark.parametrize(
    ("platform", "signal_name"),
    [("posix", "SIGTERM"), ("nt", "SIGBREAK")],
)
def test_supported_termination_signals_exit_only_after_private_volume_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    signal_name: str,
) -> None:
    _write(tmp_path, ".github/workflows/ci.yml", "jobs: {}\n")
    selected = (_input(tmp_path, ".github/workflows/ci.yml"),)
    installed: dict[int, object] = {}
    restored: list[int] = []

    class FakeOs:
        name = platform

    monkeypatch.setattr(lint, "os", FakeOs)
    if signal_name == "SIGBREAK" and not hasattr(lint.signal, "SIGBREAK"):
        monkeypatch.setattr(lint.signal, "SIGBREAK", 21, raising=False)
    selected_signal = int(getattr(lint.signal, signal_name))

    monkeypatch.setattr(lint.signal, "getsignal", lambda selected_signal: lint.signal.SIG_DFL)

    def install(selected_signal: int, handler: object) -> object:
        installed[selected_signal] = handler
        if handler == lint.signal.SIG_DFL:
            restored.append(selected_signal)
        return lint.signal.SIG_DFL

    monkeypatch.setattr(lint.signal, "signal", install)

    def create(*args: object, **kwargs: object) -> None:
        handler = installed[selected_signal]
        assert callable(handler)
        cast(Any, handler)(selected_signal, None)

    removed: list[str] = []
    monkeypatch.setattr(lint, "_create_workspace_volume", create)
    monkeypatch.setattr(
        lint,
        "_remove_workspace_volume",
        lambda docker, directory, name, token: removed.append(name),
    )

    with pytest.raises(SystemExit) as caught:
        lint._lint_in_temporary_workspace(tmp_path.resolve(), selected, "docker", tmp_path)

    assert caught.value.code == 128 + selected_signal
    assert len(removed) == 1
    assert selected_signal in restored


def test_late_container_creation_is_polled_and_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    removed = [False]
    container_id = "a" * 64

    monkeypatch.setattr(runtime, "MAX_CONTAINER_CLEANUP_SECONDS", 1.0)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        runtime,
        "_pause_cleanup_poll",
        lambda deadline: clock.__setitem__(0, min(deadline, clock[0] + 0.6)),
    )

    def listed(*args: object, **kwargs: object) -> tuple[str, ...]:
        if clock[0] < 0.5 or removed[0]:
            return ()
        return (container_id,)

    def run(command: list[str], **kwargs: object) -> lint._ProcessResult:
        assert command == ["docker", "container", "rm", "--force", container_id]
        removed[0] = True
        return _process_result(stdout=f"{container_id}\n".encode())

    monkeypatch.setattr(runtime, "_list_labeled_containers", listed)
    monkeypatch.setattr(runtime, "run_docker_control_process", run)

    runtime.remove_labeled_containers(
        "docker",
        tmp_path,
        "token",
        wait_for_late=True,
    )

    assert removed[0] is True
    assert clock[0] >= 1.0


def test_volume_cleanup_runs_after_primary_error_and_can_override_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = _write(tmp_path, ".github/workflows/ci.yml", "jobs: {}\n")
    selected = (
        WorkflowInput(
            path=workflow.resolve(),
            relative_path=".github/workflows/ci.yml",
            content=workflow.read_bytes(),
        ),
    )
    removed: list[str] = []
    monkeypatch.setattr(lint, "load_workflow_inputs", lambda repository, selections: selected)
    monkeypatch.setattr(lint, "_find_docker", lambda repository: "docker")
    monkeypatch.setattr(lint, "_create_workspace_volume", lambda *args, **kwargs: None)
    monkeypatch.setattr(lint, "_stage_workspace_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        lint,
        "_remove_workspace_volume",
        lambda docker, runtime, name, token: removed.append(name),
    )
    monkeypatch.setattr(
        lint,
        "_run_actionlint_container",
        lambda *args, **kwargs: (_ for _ in ()).throw(InputError("primary")),
    )
    with pytest.raises(InputError, match="primary"):
        lint.lint_workflows(tmp_path)
    assert len(removed) == 1

    monkeypatch.setattr(
        lint,
        "_run_actionlint_container",
        lambda *args, **kwargs: _process_result(),
    )
    monkeypatch.setattr(
        lint,
        "_remove_workspace_volume",
        lambda *args, **kwargs: (_ for _ in ()).throw(InputError("private workspace")),
    )
    with pytest.raises(InputError, match="private workspace"):
        lint.lint_workflows(tmp_path)


def test_bounded_process_terminates_spawned_descendants(tmp_path: Path) -> None:
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    child = (
        "from pathlib import Path; import time; "
        f"Path({str(started)!r}).write_text('started'); "
        "time.sleep(0.6); "
        f"Path({str(survived)!r}).write_text('survived')"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(30)"
    )

    result = lint._run_bounded_process(
        [sys.executable, "-c", parent],
        content=b"",
        cwd=tmp_path,
        timeout=0.5,
        stdout_limit=128,
        stderr_limit=128,
    )
    time.sleep(0.8)

    assert result.timed_out is True
    assert started.exists()
    assert not survived.exists()


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_process_tree_cleanup_failures_are_observable(
    platform: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 123

        def __init__(self) -> None:
            self.killed = False

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            self.killed = True

    class FakeOs:
        name = platform

        @staticmethod
        def killpg(pid: int, selected_signal: object) -> None:
            raise OSError("group cleanup failed")

    process = FakeProcess()
    monkeypatch.setattr(runtime, "os", FakeOs)
    if platform == "posix":
        monkeypatch.setattr(runtime, "signal", type("FakeSignal", (), {"SIGKILL": 9}))
    if platform == "nt":
        monkeypatch.setattr(
            runtime,
            "_close_windows_handle",
            lambda handle: (_ for _ in ()).throw(OSError("job cleanup failed")),
        )
    tree = runtime._ProcessTree(
        cast(Any, process),
        object() if platform == "nt" else None,
    )

    tree.terminate()

    assert process.killed is True
    with pytest.raises(OSError, match="bounded subprocess tree"):
        runtime._raise_process_cleanup_error(tree)


def _cached_docker_image() -> str | None:
    docker = shutil.which("docker")
    if docker is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed inspection argv
            [docker, "image", "inspect", lint.ACTIONLINT_IMAGE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return docker if result.returncode == 0 else None


def test_real_pinned_container_resolves_config_reusable_workflow_and_local_action(
    tmp_path: Path,
) -> None:
    if _cached_docker_image() is None:
        pytest.skip("the pinned actionlint image is unavailable")
    _write(
        tmp_path,
        ".github/actionlint.yaml",
        """self-hosted-runner:
  labels: [custom-runner]
config-variables: [ALLOWED]
""",
    )
    _write(
        tmp_path,
        ".github/workflows/reuse.yml",
        """name: Reuse
on:
  workflow_call:
    inputs:
      required-name:
        required: true
        type: string
jobs:
  inner:
    runs-on: custom-runner
    steps:
      - run: echo reusable
""",
    )
    _write(
        tmp_path,
        "actions/local/action.yml",
        """name: Local
description: Local action
inputs:
  token:
    description: Required token
    required: true
runs:
  using: node20
  main: dist/index.js
""",
    )
    _write(tmp_path, "actions/local/dist/index.js", b"")
    good = _write(
        tmp_path,
        ".github/workflows/good.yml",
        """name: Good
on: push
jobs:
  call:
    uses: $/.github/workflows/reuse.yml
    with:
      required-name: present
  local:
    runs-on: custom-runner
    steps:
      - uses: $/actions/local
        with:
          token: present
      - run: echo "${{ vars.ALLOWED }}"
""",
    )
    bad_action = _write(
        tmp_path,
        ".github/workflows/bad-action.yml",
        """name: Bad action
on: push
jobs:
  local:
    runs-on: custom-runner
    steps:
      - uses: $/actions/local
""",
    )
    bad_reuse = _write(
        tmp_path,
        ".github/workflows/bad-reuse.yml",
        """name: Bad reuse
on: push
jobs:
  call:
    uses: $/.github/workflows/reuse.yml
""",
    )

    report = lint.lint_workflows(
        tmp_path,
        (good, bad_action, bad_reuse),
    )
    results = {result.path: result for result in report.results}

    assert results[".github/workflows/good.yml"].valid is True
    assert {item.code for item in results[".github/workflows/bad-action.yml"].diagnostics} == {
        "actionlint.action"
    }
    assert {item.code for item in results[".github/workflows/bad-reuse.yml"].diagnostics} == {
        "actionlint.workflow-call"
    }


def test_real_pinned_container_reads_space_local_dependencies(tmp_path: Path) -> None:
    if _cached_docker_image() is None:
        pytest.skip("the pinned actionlint image is unavailable")
    _write(
        tmp_path,
        "actions/with space/action.yml",
        "name: Spaced action\nruns: {using: node20, main: index.js}\n",
    )
    _write(tmp_path, "actions/with space/index.js", b"")
    _write(tmp_path, ".github/workflows/called workflow.yml", "on: [\n")
    workflow = _write(
        tmp_path,
        ".github/workflows/ci.yml",
        """name: CI
on: push
jobs:
  call:
    uses: "./.github/workflows/called workflow.yml"
  local:
    runs-on: ubuntu-latest
    steps:
      - uses: "./actions/with space"
""",
    )

    report = lint.lint_workflows(tmp_path, (workflow,))

    diagnostics = report.results[0].diagnostics
    assert any(
        diagnostic.code == "actionlint.action" and "description is required" in diagnostic.message
        for diagnostic in diagnostics
    )
    assert any(
        diagnostic.code == "actionlint.workflow-call"
        and "error while parsing reusable workflow" in diagnostic.message
        for diagnostic in diagnostics
    )
    assert not any("could not read reusable workflow file" in item.message for item in diagnostics)


def test_real_pinned_container_accepts_no_snippet_invalid_utf8_diagnostic(tmp_path: Path) -> None:
    if _cached_docker_image() is None:
        pytest.skip("the pinned actionlint image is unavailable")
    workflow = _write(tmp_path, ".github/workflows/invalid.yml", b"\xff")

    report = lint.lint_workflows(tmp_path, (workflow,))

    assert report.valid is False
    diagnostic = report.results[0].diagnostics[0]
    assert diagnostic.code == "actionlint.syntax-check"
    assert diagnostic.line == 1
    assert diagnostic.column == 1


def test_real_pinned_container_does_not_treat_directory_presence_as_a_file(
    tmp_path: Path,
) -> None:
    if _cached_docker_image() is None:
        pytest.skip("the pinned actionlint image is unavailable")
    _write(
        tmp_path,
        "action/action.yml",
        """name: Local
description: Local
runs:
  using: node20
  main: dist
  pre: dist/.yaga-actionlint-directory
""",
    )
    (tmp_path / "action" / "dist").mkdir()
    workflow = _write(
        tmp_path,
        ".github/workflows/ci.yml",
        "name: CI\non: push\njobs: {test: {runs-on: ubuntu-latest, steps: [{uses: ./action}]}}\n",
    )

    report = lint.lint_workflows(tmp_path, (workflow,))

    assert report.valid is False
    assert "actionlint.action" in {diagnostic.code for diagnostic in report.results[0].diagnostics}
