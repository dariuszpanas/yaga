"""Build and inspect YAGA distributions with the locked backend."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[1]
MAX_SMOKE_OUTPUT_BYTES = 65_536
SMOKE_MESSAGE = "feat(build): execute the built wheel"
EXPECTED_REQUIRES_DIST = ["pyyaml<7,>=6.0.3", "typer<1,>=0.27.2"]


def validate_wheel_metadata(raw: bytes) -> None:
    """Require the wheel to advertise its complete supported runtime contract."""
    metadata = BytesParser().parsebytes(raw)
    if metadata.get_all("Requires-Dist", []) != EXPECTED_REQUIRES_DIST:
        raise SystemExit("wheel has the wrong runtime dependency metadata")
    if metadata.get("Requires-Python") != ">=3.12":
        raise SystemExit("wheel has the wrong Python requirement metadata")


def run_bounded(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """Run one smoke command with hard per-stream byte and time limits."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()

    def drain(stream: BinaryIO, target: bytearray) -> None:
        while chunk := stream.read(8192):
            remaining = MAX_SMOKE_OUTPUT_BYTES - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return

    threads = [
        threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait()
    finally:
        for thread in threads:
            thread.join()
    if timed_out:
        raise SystemExit("installed wheel CLI exceeded the smoke-test timeout")
    if overflow.is_set():
        raise SystemExit("installed wheel CLI output exceeds the smoke-test limit")
    return subprocess.CompletedProcess(command, returncode, bytes(stdout), bytes(stderr))


def successful_json(
    completed: subprocess.CompletedProcess[bytes],
    *,
    operation: str,
) -> dict[str, object]:
    """Require one installed CLI operation to succeed with UTF-8 JSON only."""
    if completed.returncode != 0:
        raise SystemExit(
            f"installed wheel CLI {operation} exited with status {completed.returncode}"
        )
    if completed.stderr:
        raise SystemExit(f"installed wheel CLI {operation} wrote to standard error")
    try:
        document = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"installed wheel CLI {operation} did not emit UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise SystemExit(f"installed wheel CLI {operation} emitted a non-object JSON report")
    return document


def exercise_installed_wheel(uv: str, output: Path, wheel: Path) -> None:
    """Install the built wheel over its hash-locked runtime and execute its CLI."""
    runtime_requirements = output / "runtime-requirements.txt"
    subprocess.run(
        [
            uv,
            "export",
            "--quiet",
            "--project",
            str(ROOT),
            "--locked",
            "--no-sources",
            "--no-default-groups",
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--python",
            sys.executable,
            "--no-python-downloads",
            "--output-file",
            str(runtime_requirements),
        ],
        cwd=ROOT,
        check=True,
    )

    environment = output / "wheel-environment"
    subprocess.run(
        [
            uv,
            "venv",
            "--no-project",
            "--no-config",
            "--no-python-downloads",
            "--python",
            sys.executable,
            str(environment),
        ],
        cwd=output,
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment),
            "--no-config",
            "--no-python-downloads",
            "--exact",
            "--require-hashes",
            "--no-build",
            "--requirements",
            str(runtime_requirements),
        ],
        cwd=output,
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(environment),
            "--no-config",
            "--no-python-downloads",
            "--no-deps",
            "--no-build",
            str(wheel.resolve()),
        ],
        cwd=output,
        check=True,
    )
    subprocess.run(
        [
            uv,
            "pip",
            "check",
            "--python",
            str(environment),
            "--no-config",
            "--no-python-downloads",
        ],
        cwd=output,
        check=True,
    )

    consumer = output / "consumer"
    consumer.mkdir()
    config = consumer / ".yaga.toml"
    executable = environment / ("Scripts/yaga.exe" if os.name == "nt" else "bin/yaga")
    if not executable.is_file():
        raise SystemExit("installed wheel does not expose the yaga executable")
    child_environment = os.environ.copy()
    for variable in (
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "YAGA_ACTION_RUNTIME",
        "YAGA_COMMIT_ACTION_RUNTIME",
    ):
        child_environment.pop(variable, None)
    child_environment["PYTHONNOUSERSITE"] = "1"
    child_environment["PYTHONSAFEPATH"] = "1"
    initialized = run_bounded(
        [
            str(executable),
            "config",
            "init",
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    initialized_document = successful_json(initialized, operation="config init")
    initialized_policy = initialized_document.get("config")
    initialized_types = (
        initialized_policy.get("allowed_types") if isinstance(initialized_policy, dict) else None
    )
    if not (
        initialized_document.get("schema_version") == 1
        and initialized_document.get("config_path") == str(config.resolve())
        and isinstance(initialized_policy, dict)
        and isinstance(initialized_types, list)
        and "feat" in initialized_types
        and initialized_policy.get("merge_commits") == "reject"
        and config.is_file()
    ):
        raise SystemExit("installed wheel CLI config init emitted the wrong report contract")

    workflow_directory = consumer / ".github" / "workflows"
    workflow_directory.mkdir(parents=True)
    (workflow_directory / "ci.yml").write_text(
        "name: CI\n"
        "on: push\n"
        "jobs:\n"
        "  check:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
        encoding="utf-8",
        newline="\n",
    )
    workflow_completed = run_bounded(
        [
            str(executable),
            "workflow",
            "check",
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    workflow_document = successful_json(workflow_completed, operation="workflow check")
    if not (
        workflow_document.get("schema_version") == 1
        and workflow_document.get("kind") == "github_workflow_policy"
        and workflow_document.get("valid") is True
        and workflow_document.get("checked") == 1
        and workflow_document.get("passed") == 1
        and workflow_document.get("failed") == 0
        and workflow_document.get("references_checked") == 1
    ):
        raise SystemExit("installed wheel CLI workflow check emitted the wrong report contract")

    repository_completed = run_bounded(
        [
            str(executable),
            "repo",
            "check",
            "--check",
            "workflow",
            "--repo",
            str(consumer),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    repository_document = successful_json(
        repository_completed,
        operation="repository check",
    )
    repository_checks = (
        repository_document.get("checks") if isinstance(repository_document, dict) else None
    )
    if not (
        repository_document.get("schema_version") == 1
        and repository_document.get("kind") == "repository_check"
        and repository_document.get("status") == "passed"
        and repository_document.get("valid") is True
        and repository_document.get("selected") == 1
        and repository_document.get("passed") == 1
        and repository_document.get("failed") == 0
        and repository_document.get("errored") == 0
        and isinstance(repository_checks, list)
        and len(repository_checks) == 1
        and isinstance(repository_checks[0], dict)
        and repository_checks[0].get("provider") == "workflow"
        and repository_checks[0].get("status") == "passed"
    ):
        raise SystemExit("installed wheel CLI repository check emitted the wrong report contract")

    lint_help = run_bounded(
        [str(executable), "workflow", "lint", "--help"],
        cwd=consumer,
        env=child_environment,
    )
    if lint_help.returncode != 0 or lint_help.stderr:
        raise SystemExit("installed wheel CLI workflow lint help failed")
    try:
        lint_help_text = lint_help.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SystemExit("installed wheel CLI workflow lint help was not UTF-8") from error
    if "pinned actionlint container" not in lint_help_text:
        raise SystemExit("installed wheel CLI workflow lint help omitted its runtime contract")

    completed = run_bounded(
        [
            str(executable),
            "commit",
            "check",
            "--message",
            SMOKE_MESSAGE,
            "--repo",
            str(consumer),
            "--config",
            str(config),
            "--format",
            "json",
        ],
        cwd=consumer,
        env=child_environment,
    )
    document = successful_json(completed, operation="commit check")
    commits = document.get("commits") if isinstance(document, dict) else None
    if not (
        isinstance(document, dict)
        and document.get("schema_version") == 1
        and document.get("valid") is True
        and document.get("checked") == 1
        and document.get("passed") == 1
        and document.get("failed") == 0
        and document.get("config_path") == str(config.resolve())
        and isinstance(commits, list)
        and len(commits) == 1
        and isinstance(commits[0], dict)
        and commits[0].get("header") == SMOKE_MESSAGE
        and commits[0].get("status") == "passed"
    ):
        raise SystemExit("installed wheel CLI emitted the wrong report contract")


def main() -> int:
    """Require one wheel and sdist containing both supported execution surfaces."""
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for the package build gate")
    with tempfile.TemporaryDirectory(prefix="yaga-build-") as temporary_directory:
        output = Path(temporary_directory)
        subprocess.run(
            [
                uv,
                "build",
                "--no-build-isolation",
                "--out-dir",
                str(output),
            ],
            cwd=ROOT,
            check=True,
        )
        wheels = list(output.glob("*.whl"))
        source_distributions = list(output.glob("*.tar.gz"))
        if len(wheels) != 1 or len(source_distributions) != 1:
            raise SystemExit("build must emit exactly one wheel and one source distribution")
        if not wheels[0].name.startswith("yaga_cli-"):
            raise SystemExit("wheel has the wrong distribution name")
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())
            required = {
                "yaga/__init__.py",
                "yaga/action_cli.py",
                "yaga/commit_action_cli.py",
                "yaga/commit_action_runtime.py",
                "yaga/cli.py",
                "yaga/codex/runtime.py",
                "yaga/commands/commit.py",
                "yaga/commands/github.py",
                "yaga/commands/repo.py",
                "yaga/commands/workflow.py",
                "yaga/commits/checker.py",
                "yaga/commits/github_event.py",
                "yaga/commits/github_reporting.py",
                "yaga/commits/service.py",
                "yaga/files.py",
                "yaga/repository/checker.py",
                "yaga/repository/models.py",
                "yaga/repository/reporting.py",
                "yaga/workflows/actionlint_runtime.py",
                "yaga/workflows/actionlint_snapshot.py",
                "yaga/workflows/checker.py",
                "yaga/workflows/inputs.py",
                "yaga/workflows/lint.py",
                "yaga/workflows/yaml.py",
            }
            if missing := sorted(required - names):
                raise SystemExit(f"wheel is missing required modules: {', '.join(missing)}")
            entry_points = next(
                (name for name in names if name.endswith(".dist-info/entry_points.txt")),
                None,
            )
            if entry_points is None:
                raise SystemExit("wheel does not contain console-script metadata")
            entry_point_text = wheel.read(entry_points).decode("utf-8")
            if "yaga = yaga.cli:main" not in entry_point_text:
                raise SystemExit("wheel does not expose the yaga console script")
            metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise SystemExit("wheel must contain exactly one metadata document")
            validate_wheel_metadata(wheel.read(metadata_paths[0]))
        with tarfile.open(source_distributions[0], mode="r:gz") as source_distribution:
            names = source_distribution.getnames()
            required_suffixes = {
                "/action.yml": "the composite Action",
                "/actions/commit-check/action.yml": "the commit-check Action",
                "/.github/workflows/commit-policy.yml": "the commit-policy dogfood workflow",
                "/.pre-commit-hooks.yaml": "the pre-commit provider manifest",
                "/examples/codex-review.yml": "the Codex review workflow example",
                "/examples/commit-policy.yml": "the commit-policy workflow example",
                "/examples/review-policy.yml": "the review policy workflow example",
                "/src/yaga/__init__.py": "the yaga package",
            }
            for suffix, label in required_suffixes.items():
                if not any(name.endswith(suffix) for name in names):
                    raise SystemExit(f"source distribution does not contain {label}")
        exercise_installed_wheel(uv, output, wheels[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
