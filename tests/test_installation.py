"""Installation isolation, offline requirements, and delegated upgrade boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yaga import config_scope, self_update
from yaga.cli import app
from yaga.commits import config
from yaga.commits.config_init import initialize_config
from yaga.errors import ConfigurationError
from yaga.version_requirement import VERSION, check_requirement


def test_scheduled_update_prints_usable_log_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shlex

    from yaga.commands import self_manage

    log = tmp_path / "user's update" / "update.log"
    monkeypatch.setattr(
        self_manage, "self_update", lambda **kwargs: self_update.UpdateResult(("uv",), log)
    )
    result = CliRunner().invoke(app, ["self", "update"])
    assert result.exit_code == 0
    assert "scheduled" in result.output
    assert "uv completed" not in result.output
    shell_command = next(
        line.split("Bash/zsh: ", 1)[1]
        for line in result.output.splitlines()
        if "Bash/zsh: " in line
    )
    assert shlex.split(shell_command) == ["cat", "--", log.as_posix()]
    assert "Get-Content -LiteralPath '" + str(log).replace("'", "''") + "'" in result.output


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows launcher handoff")
def test_windows_worker_waits_and_reports_completion(tmp_path: Path) -> None:
    worker = tmp_path / "worker.py"
    worker.write_bytes(Path(self_update.__file__).with_name("_update_worker.py").read_bytes())
    manager = tmp_path / "uv.cmd"
    manager.write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    started = time.monotonic()
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(worker), str(manager), str(sleeper.pid), str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        sleeper.wait(timeout=5)
    assert result.returncode == 0, result.stderr
    assert "YAGA_UPDATE_EXIT_CODE=0" in result.stdout
    assert time.monotonic() - started >= 0.8
    assert not worker.exists()


@pytest.mark.parametrize("requirement", [">=0.1.0", "==0.2.0", ">0.0.9, <1.0.0", "!=2.0.0"])
def test_requirement_accepts_current_release(requirement: str) -> None:
    assert check_requirement(requirement) == requirement


@pytest.mark.parametrize(
    "requirement",
    [None, 1, True, "", "*", "~=0.1.0", ">=0.1", ">=01.1.0", ">=0.1.0rc1", ">=0.1.0,"],
)
def test_requirement_rejects_invalid_syntax(requirement: object) -> None:
    with pytest.raises(ConfigurationError, match="required-version"):
        check_requirement(requirement)


@pytest.mark.parametrize("requirement", [">0.2.0", "<0.1.0", ">=1.0.0", "!=0.2.0"])
def test_requirement_rejects_incompatible_release(requirement: str) -> None:
    with pytest.raises(ConfigurationError, match="does not satisfy"):
        check_requirement(requirement)


def test_version_matches_publishable_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]
    assert VERSION == project["version"]


def test_version_failure_precedes_commit_evaluation(tmp_path: Path) -> None:
    path = tmp_path / ".yaga.toml"
    path.write_text('required-version = ">=99.0.0"\n', encoding="utf-8")
    result = CliRunner().invoke(app, ["commit", "check", "--config", str(path), "--message", "bad"])
    assert result.exit_code == 2
    assert "does not satisfy" in result.output
    assert "syntax.header" not in result.output
    assert CliRunner().invoke(app, ["self", "update", "--help"]).exit_code == 0


def test_project_replaces_global_without_reading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    global_path = tmp_path / "global.toml"
    global_path.write_text('required-version = ">=99.0.0"\n', encoding="utf-8")
    monkeypatch.setattr(config, "global_config_path", lambda: global_path)
    with pytest.raises(ConfigurationError, match="does not satisfy"):
        config.load_config(start=project)
    local = project / "pyproject.toml"
    local.write_text('[tool.yaga]\nrequired-version = ">=0.1.0"\n', encoding="utf-8")
    loaded = config.load_config(start=project)
    assert loaded.path == local
    assert loaded.policy.required_version == ">=0.1.0"
    local.write_text("[tool.yaga]\n", encoding="utf-8")
    assert config.load_config(start=project).policy.required_version is None


def test_init_ignores_conflicting_global_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_path = tmp_path / "global.toml"
    global_path.write_text('required-version = ">=99.0.0"\n', encoding="utf-8")
    monkeypatch.setattr(config, "global_config_path", lambda: global_path)
    (tmp_path / ".git").mkdir()
    assert initialize_config(tmp_path).path == tmp_path / ".yaga.toml"


@pytest.mark.parametrize("marker", [None, "uv-receipt.toml", "pipx_metadata.json"])
def test_project_venv_ignores_global_but_tools_allow_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str | None
) -> None:
    for key in ("CI", "GITHUB_ACTIONS", "YAGA_ACTION_RUNTIME", "YAGA_COMMIT_ACTION_RUNTIME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python"))
    if marker:
        (tmp_path / marker).touch()
    assert (config_scope.global_config_path() is not None) == (marker is not None)


@pytest.mark.parametrize(
    "key", ["CI", "GITHUB_ACTIONS", "YAGA_ACTION_RUNTIME", "YAGA_COMMIT_ACTION_RUNTIME"]
)
def test_automation_never_loads_global(key: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(key, "1")
    assert config_scope.global_config_path() is None


def test_self_update_refuses_project_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    with pytest.raises(ConfigurationError, match="manager of this environment"):
        self_update.self_update()


def test_self_update_checks_ownership_and_pins_tool_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "tools" / "yaga-cli"
    prefix.mkdir(parents=True)
    (prefix / "uv-receipt.toml").touch()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    executable = tmp_path / "bin" / "uv.exe"
    monkeypatch.setattr(self_update.shutil, "which", lambda _: str(executable))
    calls: list[tuple[object, dict[str, object]]] = []

    def run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess([], 0, str(prefix.parent) + "\n", "")

    monkeypatch.setattr(self_update.subprocess, "run", run)
    result = self_update.self_update(dry_run=True)
    assert result.command[-3:] == ("tool", "upgrade", "yaga-cli")
    assert len(calls) == 1
    monkeypatch.setattr(sys, "platform", "linux")
    self_update.self_update()
    assert len(calls) == 3
    environment = calls[-1][1]["env"]
    assert isinstance(environment, dict)
    assert environment["UV_TOOL_DIR"] == str(prefix.parent)

    def wrong_owner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, str(tmp_path / "other"), "")

    monkeypatch.setattr(self_update.subprocess, "run", wrong_owner)
    with pytest.raises(ConfigurationError, match="does not own"):
        self_update.self_update()
