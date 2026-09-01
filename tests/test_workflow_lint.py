"""Tests for the bounded, isolated actionlint adapter."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from yaga.errors import InputError
from yaga.workflows import actionlint_runtime as runtime
from yaga.workflows import lint
from yaga.workflows.inputs import WorkflowInput


def _workflow(path: Path, relative_path: str, content: bytes = b"jobs: {}\n") -> WorkflowInput:
    return WorkflowInput(path=path / relative_path, relative_path=relative_path, content=content)


def _diagnostic(
    path: str = ".github/workflows/ci.yml",
    *,
    message: str = "jobs must be a mapping",
    kind: str = "syntax-check",
    line: object = 3,
    column: object = 7,
) -> dict[str, object]:
    return {
        "message": message,
        "filepath": path,
        "line": line,
        "column": column,
        "kind": kind,
        "snippet": "attacker-controlled source is deliberately discarded",
        "end_column": 8,
    }


def _result(
    returncode: int,
    document: object = None,
    *,
    stderr: bytes = b"",
    stdout_overflow: bool = False,
    stderr_overflow: bool = False,
    timed_out: bool = False,
) -> lint._ProcessResult:
    if document is None:
        document = []
    return lint._ProcessResult(
        returncode=returncode,
        stdout=json.dumps(document).encode(),
        stderr=stderr,
        stdout_overflow=stdout_overflow,
        stderr_overflow=stderr_overflow,
        timed_out=timed_out,
    )


def _mock_runtime(
    monkeypatch: pytest.MonkeyPatch,
    workflows: tuple[WorkflowInput, ...],
    runner: Callable[..., lint._ProcessResult],
) -> None:
    monkeypatch.setattr(lint, "_find_docker", lambda repository: "C:/tools/docker.exe")
    monkeypatch.setattr(lint, "load_workflow_inputs", lambda repository, selections: workflows)
    monkeypatch.setattr(lint, "_create_workspace_volume", lambda *args, **kwargs: None)
    monkeypatch.setattr(lint, "_stage_workspace_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(lint, "_remove_workspace_volume", lambda *args, **kwargs: None)
    monkeypatch.setattr(lint, "_run_actionlint_container", runner)


def test_lint_uses_the_pinned_isolated_container_without_a_host_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"name: CI\non: push\njobs: {}\n"
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir()
    captured: dict[str, Any] = {"removed": False}

    def create(
        docker: str,
        runtime: Path,
        token: str,
        container_name: str,
        command: list[str],
        *,
        timeout: float,
    ) -> str:
        captured["create"] = command
        captured["create_timeout"] = timeout
        assert docker == "C:/tools/docker.exe"
        assert runtime == runtime_directory
        assert token == "a" * 32
        assert container_name == "yaga-actionlint-lint-" + "a" * 32
        return "b" * 64

    def run(command: list[str], **kwargs: object) -> lint._ProcessResult:
        captured["start"] = command
        captured.update(kwargs)
        return _result(0)

    def remove(
        docker: str,
        runtime: Path,
        token: str,
        *,
        container_name: str | None = None,
    ) -> None:
        assert docker == "C:/tools/docker.exe"
        assert runtime == runtime_directory
        assert token == "a" * 32
        assert container_name == "yaga-actionlint-lint-" + "a" * 32
        captured["removed"] = True

    monkeypatch.setattr(runtime.secrets, "token_hex", lambda size: "a" * 32)
    monkeypatch.setattr(runtime, "create_labeled_container", create)
    monkeypatch.setattr(runtime, "run_bounded_process", run)
    monkeypatch.setattr(runtime, "remove_labeled_containers", remove)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / ".docker"))

    result = runtime.run_actionlint_container(
        "C:/tools/docker.exe",
        runtime_directory,
        "yaga-actionlint-volume",
        "examples/ci.yml",
        content=content,
        config_path=None,
        timeout=10,
    )

    assert result.returncode == 0
    assert captured["create"] == [
        "C:/tools/docker.exe",
        "create",
        "--name",
        "yaga-actionlint-lint-" + "a" * 32,
        "--label",
        "io.yaga.actionlint.run=" + "a" * 32,
        "--interactive",
        "--network",
        "none",
        "--read-only",
        "--log-driver",
        "none",
        "--user",
        "65534:65534",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--cpus",
        "1",
        "--mount",
        "type=volume,source=yaga-actionlint-volume,target=/workspace,readonly,volume-nocopy",
        "--workdir",
        "/workspace",
        lint.ACTIONLINT_IMAGE,
        "-no-color",
        "-format",
        "{{json .}}",
        "-stdin-filename",
        "examples/ci.yml",
        "-",
    ]
    assert captured["start"] == [
        "C:/tools/docker.exe",
        "start",
        "--attach",
        "--interactive",
        "b" * 64,
    ]
    assert "-v" not in captured["create"]
    assert str(tmp_path.resolve()) not in captured["create"]
    assert captured["content"] == content
    assert captured["cwd"] == runtime_directory
    assert captured["stdout_limit"] == lint.MAX_ACTIONLINT_STDOUT_BYTES
    assert captured["stderr_limit"] == lint.MAX_ACTIONLINT_STDERR_BYTES
    assert 0 < captured["timeout"] <= lint.MAX_ACTIONLINT_SECONDS
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert str(tmp_path.resolve()) not in environment["PATH"]
    assert "DOCKER_CONFIG" not in environment
    assert captured["removed"] is True


def test_findings_are_validated_sorted_sanitized_and_snippets_are_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ".github/workflows/ci.yml"
    findings = [
        _diagnostic(
            path,
            message="z\x1b\n::error::" + "m" * 3000,
            kind="Syntax Check!\n",
            line=8,
            column=4,
        ),
        _diagnostic(path, message="first", kind="expression", line=2, column=3),
    ]
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, path),),
        lambda *args, **kwargs: _result(1, findings),
    )

    report = lint.lint_workflows(tmp_path)

    diagnostics = report.results[0].diagnostics
    assert [(item.line, item.column) for item in diagnostics] == [(2, 3), (8, 4)]
    assert diagnostics[0].code == "actionlint.expression"
    assert diagnostics[1].code == "actionlint.syntax-check"
    assert "\n" not in diagnostics[1].message
    assert "\x1b" not in diagnostics[1].message
    assert len(diagnostics[1].message) == lint.MAX_ACTIONLINT_MESSAGE_CHARS
    assert "snippet" not in diagnostics[1].message


@pytest.mark.parametrize(
    ("returncode", "document"),
    [
        (0, [_diagnostic()]),
        (1, []),
    ],
)
def test_exit_status_must_agree_with_the_diagnostic_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    document: object,
) -> None:
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, ".github/workflows/ci.yml"),),
        lambda *args, **kwargs: _result(returncode, document),
    )

    with pytest.raises(InputError, match="inconsistent"):
        lint.lint_workflows(tmp_path)


@pytest.mark.parametrize("returncode", [-9, 2, 3, 127])
def test_non_lint_exit_status_is_an_operational_error_without_raw_output_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, ".github/workflows/ci.yml"),),
        lambda *args, **kwargs: _result(
            returncode,
            stderr=b"docker failed\n::error:: attacker\x00text",
        ),
    )

    with pytest.raises(InputError) as caught:
        lint.lint_workflows(tmp_path)

    message = str(caught.value)
    assert "\n" not in message
    assert "\x00" not in message
    assert "docker failed?::error:: attacker?text" in message


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ({}, "must be a list"),
        (["not a mapping"], "malformed diagnostic"),
        ([_diagnostic("elsewhere.yml")], "unexpected diagnostic path"),
        ([{**_diagnostic(), "message": 1}], "malformed diagnostic field"),
        ([{**_diagnostic(), "kind": None}], "malformed diagnostic field"),
        ([_diagnostic(line=True)], "invalid diagnostic line"),
        ([_diagnostic(line=-1)], "invalid diagnostic line"),
        ([_diagnostic(column=-1)], "invalid diagnostic column"),
        ([_diagnostic(column=lint.MAX_ACTIONLINT_POSITION + 1)], "invalid diagnostic column"),
    ],
)
def test_malformed_actionlint_diagnostics_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: object,
    match: str,
) -> None:
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, ".github/workflows/ci.yml"),),
        lambda *args, **kwargs: _result(1, document),
    )

    with pytest.raises(InputError, match=match):
        lint.lint_workflows(tmp_path)


@pytest.mark.parametrize(
    ("line", "column", "expected"),
    [
        (0, 0, (1, 1)),
        (0, 6, (1, 6)),
        (3, 0, (3, 1)),
    ],
)
def test_zero_actionlint_positions_are_normalized_to_one_based_coordinates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    line: int,
    column: int,
    expected: tuple[int, int],
) -> None:
    path = ".github/workflows/ci.yml"
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, path),),
        lambda *args, **kwargs: _result(
            1,
            [_diagnostic(path, line=line, column=column)],
        ),
    )

    diagnostic = lint.lint_workflows(tmp_path).results[0].diagnostics[0]

    assert (diagnostic.line, diagnostic.column) == expected


@pytest.mark.parametrize(
    ("output", "match"),
    [
        (b"not-json", "malformed JSON"),
        (b"\xff", "malformed JSON"),
    ],
)
def test_malformed_json_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    match: str,
) -> None:
    result = _result(1)
    result = lint._ProcessResult(
        returncode=result.returncode,
        stdout=output,
        stderr=result.stderr,
        stdout_overflow=False,
        stderr_overflow=False,
        timed_out=False,
    )
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, ".github/workflows/ci.yml"),),
        lambda *args, **kwargs: result,
    )

    with pytest.raises(InputError, match=match):
        lint.lint_workflows(tmp_path)


def test_per_file_and_aggregate_diagnostic_counts_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ".github/workflows/ci.yml"
    monkeypatch.setattr(lint, "MAX_ACTIONLINT_DIAGNOSTICS", 1)
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, path),),
        lambda *args, **kwargs: _result(1, [_diagnostic(path), _diagnostic(path)]),
    )
    with pytest.raises(InputError, match="diagnostic limit"):
        lint.lint_workflows(tmp_path)

    workflows = (
        _workflow(tmp_path, ".github/workflows/a.yml"),
        _workflow(tmp_path, ".github/workflows/b.yml"),
    )

    def one_finding(
        _docker: str,
        _runtime_directory: Path,
        _volume_name: str,
        relative_path: str,
        **kwargs: object,
    ) -> lint._ProcessResult:
        return _result(1, [_diagnostic(relative_path)])

    _mock_runtime(monkeypatch, workflows, one_finding)
    with pytest.raises(InputError, match="diagnostic total limit"):
        lint.lint_workflows(tmp_path)


@pytest.mark.parametrize(
    ("result", "match"),
    [
        (_result(1, stdout_overflow=True), "output exceeds"),
        (_result(1, stderr_overflow=True), "error output exceeds"),
        (_result(1, timed_out=True), "timed out"),
    ],
)
def test_runtime_resource_failures_are_operational_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: lint._ProcessResult,
    match: str,
) -> None:
    _mock_runtime(
        monkeypatch,
        (_workflow(tmp_path, ".github/workflows/ci.yml"),),
        lambda *args, **kwargs: result,
    )

    with pytest.raises(InputError, match=match):
        lint.lint_workflows(tmp_path)


def test_missing_or_unstartable_docker_is_an_operational_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_workflow = _workflow(tmp_path, ".github/workflows/ci.yml")
    monkeypatch.setattr(
        lint,
        "load_workflow_inputs",
        lambda repository, selections: (missing_workflow,),
    )
    monkeypatch.setattr(lint, "_find_docker", lambda repository: None)
    with pytest.raises(InputError, match="Docker is required"):
        lint.lint_workflows(tmp_path)

    workflow = _workflow(tmp_path, ".github/workflows/ci.yml")

    def cannot_start(*args: object, **kwargs: object) -> lint._ProcessResult:
        raise OSError("raw host detail\nshould not leak")

    _mock_runtime(monkeypatch, (workflow,), cannot_start)
    with pytest.raises(InputError) as caught:
        lint.lint_workflows(tmp_path)
    assert "raw host detail" not in str(caught.value)


def test_bounded_process_transfers_stdin_without_a_shell(tmp_path: Path) -> None:
    payload = b"literal $HOME && echo not-a-shell\n"
    result = lint._run_bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        content=payload,
        cwd=tmp_path,
        timeout=5,
        stdout_limit=1024,
        stderr_limit=1024,
    )

    assert result.returncode == 0
    assert result.stdout == payload
    assert result.stderr == b""
    assert result.stdout_overflow is False
    assert result.timed_out is False


@pytest.mark.parametrize(
    ("file_descriptor", "overflow_attribute", "output_attribute"),
    [(1, "stdout_overflow", "stdout"), (2, "stderr_overflow", "stderr")],
)
def test_process_capture_enforces_streaming_pipe_bounds(
    tmp_path: Path,
    file_descriptor: int,
    overflow_attribute: str,
    output_attribute: str,
) -> None:
    result = lint._run_bounded_process(
        [
            sys.executable,
            "-c",
            f"import os; os.write({file_descriptor}, b'x' * 4096)",
        ],
        content=b"",
        cwd=tmp_path,
        timeout=5,
        stdout_limit=128,
        stderr_limit=128,
    )

    assert getattr(result, overflow_attribute)
    assert len(getattr(result, output_attribute)) == 128


def test_process_timeout_is_hard_bounded(tmp_path: Path) -> None:
    result = lint._run_bounded_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        content=b"",
        cwd=tmp_path,
        timeout=0.05,
        stdout_limit=128,
        stderr_limit=128,
    )

    assert result.timed_out is True
    assert result.returncode != 0


def test_real_pinned_container_reports_a_bounded_finding_when_cached(tmp_path: Path) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is unavailable")
    try:
        cached = subprocess.run(  # noqa: S603 - fixed Docker inspection argv
            [docker, "image", "inspect", lint.ACTIONLINT_IMAGE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker is unavailable")
    if cached.returncode != 0:
        pytest.skip("the pinned actionlint image is not cached")

    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs: [\n", encoding="utf-8")

    report = lint.lint_workflows(tmp_path)

    assert report.valid is False
    assert report.results[0].diagnostics[0].code == "actionlint.syntax-check"
    assert report.results[0].diagnostics[0].line >= 1
    assert report.results[0].diagnostics[0].column == 1
    assert len(report.results[0].diagnostics[0].message) <= lint.MAX_ACTIONLINT_MESSAGE_CHARS
