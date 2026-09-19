"""Exercise delivered scripts without downloading or changing the user's installation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("available", ["both", "curl", "wget", "neither"])
@pytest.mark.parametrize("download_fails", [False, True])
def test_shell_bootstrap_downloader_selection(
    tmp_path: Path, available: str, download_fails: bool
) -> None:
    executable = (
        "C:/Program Files/Git/bin/bash.exe" if sys.platform == "win32" else shutil.which("sh")
    )
    if not executable or not Path(executable).is_file():
        pytest.skip("POSIX shell is unavailable")
    wrapper = r"""
command() {
    case "$*" in
        '-v curl') [ "$YAGA_TEST_AVAILABLE" = both ] || [ "$YAGA_TEST_AVAILABLE" = curl ] ;;
        '-v wget') [ "$YAGA_TEST_AVAILABLE" = both ] || [ "$YAGA_TEST_AVAILABLE" = wget ] ;;
        *) return 1 ;;
    esac
}
download() {
    printf '%s\n' "$1" >> "$YAGA_TEST_LOG"
    shift
    destination=
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --output|--output-document) shift; destination=$1 ;;
        esac
        shift
    done
    printf '%s\n' 'echo bootstrap-reached' 'exit 23' > "$destination"
    [ "$YAGA_TEST_DOWNLOAD_FAILS" != 1 ] || return 7
}
curl() { download curl "$@"; }
wget() { download wget "$@"; }
. "$YAGA_INSTALLER_SOURCE"
"""
    log = tmp_path / "downloads.txt"
    environment = dict(
        os.environ,
        YAGA_INSTALLER_SOURCE=(ROOT / "docs/install.sh").as_posix(),
        YAGA_TEST_LOG=log.as_posix(),
        YAGA_TEST_AVAILABLE=available,
        YAGA_TEST_DOWNLOAD_FAILS="1" if download_fails else "0",
    )
    environment.pop("YAGA_VERSION", None)
    result = subprocess.run(
        [executable, "-c", wrapper],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if available == "neither":
        assert result.returncode == 2
        assert "requires curl or wget" in result.stderr
        assert not log.exists()
    else:
        assert log.read_text().splitlines() == ["wget" if available == "wget" else "curl"]
        assert result.returncode == (7 if download_fails else 23)
        assert ("bootstrap-reached" in result.stdout) == (not download_fails)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows execution policy")
def test_piped_powershell_bootstrap_works_with_restricted_file_policy(tmp_path: Path) -> None:
    executable = shutil.which("powershell") or shutil.which("pwsh")
    assert executable is not None
    environment = dict(os.environ, YAGA_INSTALLER_SOURCE=str(ROOT / "docs/install.ps1"))
    environment.pop("YAGA_VERSION", None)
    command = r"""
function Get-Command { param($Name, $CommandType, $ErrorAction) return $null }
function Invoke-WebRequest {
    param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
    [IO.File]::WriteAllText($OutFile, 'throw "bootstrap reached"')
}
Invoke-Expression ([IO.File]::ReadAllText($env:YAGA_INSTALLER_SOURCE))
"""
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Restricted",
            "-Command",
            command,
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0  # The mock stops before installation or network access.
    assert "bootstrap reached" in result.stderr
    assert "running scripts is disabled" not in result.stderr


@pytest.mark.parametrize("shell", ["sh", "powershell"])
@pytest.mark.parametrize("scenario", ["latest", "pinned", "invalid", "failed", "no-path"])
def test_installer_delegation(tmp_path: Path, shell: str, scenario: str) -> None:
    if shell == "powershell":
        if sys.platform != "win32":
            pytest.skip("Windows installer is exercised on Windows")
        executable = shutil.which("pwsh") or shutil.which("powershell")
        suffix = ".ps1"
        arguments = ["-NoProfile", "-NonInteractive", "-File"]
        mock = tmp_path / "uv.cmd"
        mock.write_text(
            '@echo off\necho %*>> "%YAGA_TEST_LOG%"\nexit /b %YAGA_TEST_EXIT%\n',
            encoding="utf-8",
        )
    else:
        executable = (
            "C:/Program Files/Git/bin/bash.exe" if sys.platform == "win32" else shutil.which("sh")
        )
        suffix = ".sh"
        arguments = []
        mock = tmp_path / "uv"
        mock.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$YAGA_TEST_LOG"\nexit "$YAGA_TEST_EXIT"\n',
            encoding="utf-8",
        )
        mock.chmod(0o755)
    if not executable or not Path(executable).is_file():
        pytest.skip(f"{shell} is unavailable")
    log = tmp_path / "calls.txt"
    environment = dict(os.environ)
    environment.update(
        PATH=str(tmp_path) + os.pathsep + environment.get("PATH", ""),
        YAGA_TEST_LOG=log.as_posix(),
        YAGA_TEST_EXIT="7" if scenario == "failed" else "0",
        YAGA_VERSION={"pinned": "1.2.3", "invalid": "1.2.3;echo bad"}.get(scenario, ""),
        YAGA_NO_MODIFY_PATH="1" if scenario == "no-path" else "0",
    )
    result = subprocess.run(
        [executable, *arguments, str(ROOT / "docs" / ("install" + suffix))],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if scenario == "invalid":
        assert result.returncode != 0
        assert not log.exists()
    elif scenario == "failed":
        assert result.returncode != 0
        assert "YAGA installed" not in result.stdout
        assert "update-shell" not in log.read_text()
    else:
        assert result.returncode == 0, result.stderr
        calls = log.read_text().splitlines()
        package = "yaga-cli==1.2.3" if scenario == "pinned" else "yaga-cli"
        assert calls[0] == f"--no-config tool install --python 3.12 {package}"
        assert len(calls) == (1 if scenario == "no-path" else 2)
        if scenario != "no-path":
            assert calls[1] == "--no-config tool update-shell"
