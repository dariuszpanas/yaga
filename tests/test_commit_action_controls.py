"""Execute the hosted control shell in an isolated copy, without runner context overrides."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("valid", 0),
        ("invalid", 1),
        ("stale", 2),
        ("nondefault", 0),
        ("nondefault-invalid", 1),
        ("advanced-main", 0),
    ],
)
def test_composite_control_shell(tmp_path: Path, scenario: str, expected: int) -> None:
    bash = "C:/Program Files/Git/bin/bash.exe" if sys.platform == "win32" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("Bash is unavailable")
    workspace = tmp_path / "workspace"
    action_dir = workspace / "actions/commit-check"
    action_dir.mkdir(parents=True)
    shutil.copyfile(ROOT / "actions/commit-check/action.yml", action_dir / "action.yml")
    shutil.copytree(ROOT / "src", workspace / "src", ignore=shutil.ignore_patterns("__pycache__"))
    output = tmp_path / "output"
    environment = dict(
        os.environ,
        RUNNER_TEMP=str(tmp_path),
        GITHUB_WORKSPACE=str(workspace),
        GITHUB_OUTPUT=output.as_posix(),
        GITHUB_ACTION_PATH=action_dir.as_posix(),
        YAGA_COMMIT_TRUSTED_CONFIG=".yaga.toml",
        YAGA_COMMIT_ACTION_RUNTIME="1",
        PYTHONUTF8="1",
    )
    environment.pop("YAGA_ACTION_RUNTIME", None)
    environment.pop("GITHUB_STEP_SUMMARY", None)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare_commit_action_control.py"), scenario],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    manifest = yaml.safe_load((action_dir / "action.yml").read_text("utf-8"))
    script = manifest["runs"]["steps"][-1]["run"]
    result = subprocess.run(
        [bash, "-c", script],
        env=environment,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert f"exit-code={expected}" in output.read_text("utf-8")
