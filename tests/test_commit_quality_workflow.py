"""Execute the quality workflow's shell adapter without downloading a model."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from yaga.commits.git import read_commit, read_range

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/commit-quality.yml"


@pytest.fixture
def bash() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            for parent in Path(git).resolve().parents:
                candidate = parent / "bin/bash.exe"
                if candidate.is_file():
                    return str(candidate)
    else:
        executable = shutil.which("bash")
        if executable:
            return executable
    pytest.skip("executing the hosted shell adapter requires Bash")


def run_workflow(
    bash: str,
    tmp_path: Path,
    *,
    event: str = "pull_request",
    base: str = "a" * 40,
    head: str = "b" * 40,
    online: int = 0,
    offline: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    script = next(
        step["run"]
        for step in workflow["jobs"]["quality"]["steps"]
        if step.get("name") == "Populate cache and prove offline reuse"
    )
    script = script.replace("${{ steps.model-cache.outputs.cache-hit }}", "false")
    wrapper = tmp_path / "quality-workflow.sh"
    wrapper.write_text(
        "uv() {\n"
        '  printf "%s\\0" "$@" >> "$YAGA_TEST_ARGUMENTS"\n'
        '  printf "\\n" >> "$YAGA_TEST_ARGUMENTS"\n'
        '  printf "Provider output <value>&\\n"\n'
        '  for argument in "$@"; do\n'
        '    if [[ "$argument" == --offline ]]; then return "$YAGA_TEST_OFFLINE"; fi\n'
        "  done\n"
        '  return "$YAGA_TEST_ONLINE"\n'
        "}\n" + script,
        encoding="utf-8",
        newline="\n",
    )
    arguments_file = tmp_path / "arguments.bin"
    summary = tmp_path / "summary.md"
    environment = {
        **os.environ,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_STEP_SUMMARY": summary.as_posix(),
        "YAGA_QUALITY_BASE_SHA": base,
        "YAGA_QUALITY_HEAD_SHA": head,
        "YAGA_QUALITY_TASK": "classification",
        "YAGA_QUALITY_MODEL_ID": "audit/model",
        "YAGA_QUALITY_MODEL_REVISION": "c" * 40,
        "YAGA_TEST_ARGUMENTS": arguments_file.as_posix(),
        "YAGA_TEST_ONLINE": str(online),
        "YAGA_TEST_OFFLINE": str(offline),
    }
    completed = subprocess.run(
        [bash, "--noprofile", "--norc", wrapper.as_posix()],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    calls = [
        line.removesuffix(b"\0").decode().split("\0")
        for line in arguments_file.read_bytes().splitlines()
    ]
    assert len(calls) == 2
    assert "--offline" not in calls[0]
    assert "--offline" in calls[1]
    summary_text = summary.read_text(encoding="utf-8")
    assert "(online)" in summary_text
    assert "(offline replay)" in summary_text
    assert summary_text.count("Provider output &lt;value&gt;&amp;") == 2
    return completed, calls


@pytest.mark.parametrize(
    ("online", "offline", "expected"),
    [
        (0, 0, 0),
        (1, 0, 1),
        (0, 1, 1),
        (1, 1, 1),
        (2, 0, 2),
        (0, 2, 2),
        (137, 0, 2),
        (0, 137, 2),
        (127, 127, 2),
        (137, 1, 2),
        (1, 137, 2),
    ],
)
def test_quality_workflow_replays_after_findings_and_fails_on_process_errors(
    bash: str, tmp_path: Path, online: int, offline: int, expected: int
) -> None:
    completed, _ = run_workflow(bash, tmp_path, online=online, offline=offline)

    assert completed.returncode == expected, completed.stderr


def git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "user.name=YAGA Tests", "-c", "user.email=yaga@example.invalid", *arguments],
        cwd=repository,
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


@pytest.mark.parametrize("event", ["pull_request", "workflow_dispatch"])
def test_quality_workflow_selects_only_the_intended_commits(
    bash: str, tmp_path: Path, event: str
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "commit", "--allow-empty", "--quiet", "--message", "chore: baseline")
    git(repository, "branch", "feature")
    git(
        repository, "commit", "--allow-empty", "--quiet", "--message", "docs: unrelated base change"
    )
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "--quiet", "feature")
    git(repository, "commit", "--allow-empty", "--quiet", "--message", "fix: feature change")
    head = git(repository, "rev-parse", "HEAD")

    completed, calls = run_workflow(bash, tmp_path, event=event, base=base, head=head)

    assert completed.returncode == 0, completed.stderr
    for arguments in calls:
        if event == "pull_request":
            selection = arguments[arguments.index("--range") + 1]
            selected = read_range(repository, selection, max_commits=256)
        else:
            selection = arguments[arguments.index("--commit") + 1]
            selected = [read_commit(repository, selection)]
        assert [target.sha for target in selected] == [head]
