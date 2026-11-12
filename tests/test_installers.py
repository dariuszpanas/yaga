"""Exercise delivered scripts without downloading or changing the user's installation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from yaga.errors import safe_error_text

ROOT = Path(__file__).resolve().parents[1]
_MAX_DIAGNOSTIC_STREAM_CHARS = 512
_SECRET_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(authorization\s*:\s*(?:bearer\s+)?|bearer\s+|"
    r"(?:token|secret|password|passwd|api[_-]?key)\s*[=:]\s*)([^\s'\";,]+)"
)
_URL_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_HOST_USER_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:[/\\]|/[a-z]/)users[/\\][^/\\\s:'\"]+|/(?:home|Users)/[^/\s:'\"]+"
)
_PYTEST_USER_DIR_RE = re.compile(r"pytest-of-[^/\\\s:'\"]+")
_SHELL_BOOTSTRAP_WRAPPER = r"""
printf '%s\n' wrapper-start >> "$YAGA_TEST_STAGE_LOG"
command() {
    case "$*" in
        '-v curl') [ "$YAGA_TEST_AVAILABLE" = both ] || [ "$YAGA_TEST_AVAILABLE" = curl ] ;;
        '-v wget') [ "$YAGA_TEST_AVAILABLE" = both ] || [ "$YAGA_TEST_AVAILABLE" = wget ] ;;
        *) return 1 ;;
    esac
}
download() {
    printf 'download-enter:%s\n' "$1" >> "$YAGA_TEST_STAGE_LOG"
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
    printf 'download-wrote-destination\n' >> "$YAGA_TEST_STAGE_LOG"
    [ "$YAGA_TEST_DOWNLOAD_FAILS" != 1 ] || return 7
}
curl() { download curl "$@"; }
wget() { download wget "$@"; }
printf '%s\n' before-source >> "$YAGA_TEST_STAGE_LOG"
. "$YAGA_INSTALLER_SOURCE"
"""


def _path_variants(path: Path) -> tuple[str, ...]:
    posix = path.as_posix().rstrip("/")
    windows = str(path).rstrip("\\/")
    variants = {posix, windows}
    if len(posix) >= 3 and posix[1:3] == ":/":
        drive = posix[0].lower()
        variants.add(f"/{drive}/{posix[3:]}")
    try:
        rel_tmp = path.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        pass
    else:
        rel_posix = rel_tmp.as_posix().rstrip("/")
        variants.add(f"/tmp/{rel_posix}" if rel_posix and rel_posix != "." else "/tmp")
    return tuple(sorted((item for item in variants if item), key=len, reverse=True))


def _sanitize_diagnostic_stream(
    value: str,
    *,
    tmp_path: Path,
    maximum: int = _MAX_DIAGNOSTIC_STREAM_CHARS,
) -> str:
    redacted = value
    replacements: list[tuple[str, str]] = []
    for candidate in _path_variants(tmp_path):
        replacements.append((candidate, "<tmp>"))
    for candidate in _path_variants(ROOT):
        replacements.append((candidate, "<repo>"))
    home = Path.home()
    if home != Path("/"):
        for candidate in _path_variants(home):
            replacements.append((candidate, "<home>"))
    for raw_prefix, placeholder in sorted(
        replacements, key=lambda pair: len(pair[0]), reverse=True
    ):
        redacted = re.sub(re.escape(raw_prefix), placeholder, redacted, flags=re.IGNORECASE)
    redacted = _URL_USERINFO_RE.sub("://<redacted>@", redacted)
    redacted = _SECRET_TOKEN_RE.sub("<redacted-token>", redacted)
    redacted = _SECRET_KV_RE.sub(r"\1<redacted>", redacted)
    redacted = _HOST_USER_PATH_RE.sub("<user-home>", redacted)
    redacted = _PYTEST_USER_DIR_RE.sub("pytest-of-<user>", redacted)
    return safe_error_text(redacted, maximum=maximum)


def _classify_bootstrap_stage(stages: list[str], *, log_exists: bool) -> str:
    if not stages:
        return "shell-startup"
    if "before-source" not in stages:
        return "wrapper-init"
    if not any(stage.startswith("download-enter:") for stage in stages):
        return "installer-pre-download"
    if not log_exists:
        return "download-log-write"
    if "download-wrote-destination" not in stages:
        return "download-destination-write"
    return "post-download"


def _format_bootstrap_diagnostic(
    *,
    available: str,
    download_fails: bool,
    executable: str,
    returncode: int,
    stdout: str,
    stderr: str,
    stages: list[str],
    log_exists: bool,
    tmp_path: Path,
) -> str:
    shell_id = f"{sys.platform}:{Path(executable).name}"
    stage = _classify_bootstrap_stage(stages, log_exists=log_exists)
    safe_stages = [
        _sanitize_diagnostic_stream(item, tmp_path=tmp_path, maximum=64) for item in stages[:16]
    ]
    safe_stdout = _sanitize_diagnostic_stream(stdout, tmp_path=tmp_path)
    safe_stderr = _sanitize_diagnostic_stream(stderr, tmp_path=tmp_path)
    return (
        f"shell bootstrap diagnostic: "
        f"case=(available={available}, download_fails={download_fails}) "
        f"shell={shell_id} "
        f"stage={stage} "
        f"returncode={returncode} "
        f"log_exists={log_exists} "
        f"stages={safe_stages} "
        f"stdout={safe_stdout!r} "
        f"stderr={safe_stderr!r}"
    )


def _assert_shell_bootstrap_case(
    tmp_path: Path,
    *,
    executable: str,
    available: str,
    download_fails: bool,
    installer_source: Path = ROOT / "docs/install.sh",
) -> None:
    log = tmp_path / "downloads.txt"
    stage_log = tmp_path / "stages.txt"
    environment = dict(
        os.environ,
        YAGA_INSTALLER_SOURCE=installer_source.as_posix(),
        YAGA_TEST_LOG=log.as_posix(),
        YAGA_TEST_STAGE_LOG=stage_log.as_posix(),
        YAGA_TEST_AVAILABLE=available,
        YAGA_TEST_DOWNLOAD_FAILS="1" if download_fails else "0",
    )
    environment.pop("YAGA_VERSION", None)
    result = subprocess.run(
        [executable, "-c", _SHELL_BOOTSTRAP_WRAPPER],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    stages = stage_log.read_text(encoding="utf-8").splitlines() if stage_log.is_file() else []
    log_exists = log.is_file()
    diagnostic = _format_bootstrap_diagnostic(
        available=available,
        download_fails=download_fails,
        executable=executable,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        stages=stages,
        log_exists=log_exists,
        tmp_path=tmp_path,
    )
    if available == "neither":
        assert result.returncode == 2, diagnostic
        assert "requires curl or wget" in result.stderr, diagnostic
        assert not log_exists, diagnostic
    else:
        assert log_exists, diagnostic
        assert log.read_text(encoding="utf-8").splitlines() == [
            "wget" if available == "wget" else "curl"
        ], diagnostic
        assert result.returncode == (7 if download_fails else 23), diagnostic
        assert ("bootstrap-reached" in result.stdout) == (not download_fails), diagnostic


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
    _assert_shell_bootstrap_case(
        tmp_path,
        executable=executable,
        available=available,
        download_fails=download_fails,
    )


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


@pytest.mark.parametrize("shell", ["sh", "msys-zsh", "powershell"])
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
        if shell == "msys-zsh":
            if sys.platform != "win32":
                pytest.skip("MSYS2 zsh requires Windows")
            executable = "C:/msys64/usr/bin/zsh.exe"
            arguments = ["-f"]
        mock = tmp_path / "uv"
        mock.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$YAGA_TEST_LOG"\n'
            'if [ "$*" = "--no-config tool dir --bin" ]; then printf "%s\\n" "$YAGA_TEST_BIN"; fi\n'
            'exit "$YAGA_TEST_EXIT"\n',
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
        YAGA_TEST_BIN=(tmp_path / "user's tool bin").as_posix(),
        YAGA_TEST_EXIT="7" if scenario == "failed" else "0",
        YAGA_VERSION={"pinned": "1.2.3", "invalid": "1.2.3;echo bad"}.get(scenario, ""),
        YAGA_NO_MODIFY_PATH="1" if scenario == "no-path" else "0",
    )
    if shell == "msys-zsh":
        environment["PATH"] = str(tmp_path) + ";C:/msys64/usr/bin;" + os.environ.get("PATH", "")
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
        msys_shell = shell in {"sh", "msys-zsh"} and sys.platform == "win32"
        assert len(calls) == (1 if scenario == "no-path" and not msys_shell else 2)
        if msys_shell:
            assert calls[1] == "--no-config tool dir --bin"
            assert "export PATH='" in result.stdout
            assert "tool bin'" in result.stdout
            assert "~/.zshrc" in result.stdout
            assert "Open a new terminal" not in result.stdout
            tool_bin = tmp_path / "user's tool bin"
            tool_bin.mkdir()
            entrypoint = tool_bin / "yaga"
            entrypoint.write_text("#!/bin/sh\necho found-test-yaga\n", encoding="utf-8")
            entrypoint.chmod(0o755)
            export = next(
                line.strip() for line in result.stdout.splitlines() if "export PATH=" in line
            )
            resolved = subprocess.run(
                [executable, "-c", export + "; hash -r; yaga"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert resolved.returncode == 0, resolved.stderr
            assert resolved.stdout.strip() == "found-test-yaga"
        elif scenario != "no-path":
            assert calls[1] == "--no-config tool update-shell"


def test_shell_bootstrap_missing_log_fails_closed_with_sanitized_stage_diagnostics(
    tmp_path: Path,
) -> None:
    executable = (
        "C:/Program Files/Git/bin/bash.exe" if sys.platform == "win32" else shutil.which("sh")
    )
    if not executable or not Path(executable).is_file():
        pytest.skip("POSIX shell is unavailable")
    failing_installer = tmp_path / "failing-install.sh"
    failing_installer.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'printf "mktemp failed in %s home=%s pat=ghp_SecretToken12345 secret=topSecret99 \\033[31mboom\\n" '
        '"$PWD" "$HOME" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError) as exc_info:
        _assert_shell_bootstrap_case(
            tmp_path,
            executable=executable,
            available="both",
            download_fails=True,
            installer_source=failing_installer,
        )
    message = str(exc_info.value)
    assert "case=(available=both, download_fails=True)" in message
    assert f"shell={sys.platform}:{Path(executable).name}" in message
    assert "stage=installer-pre-download" in message
    assert "returncode=1" in message
    assert "log_exists=False" in message
    assert "stages=['wrapper-start', 'before-source']" in message
    assert "<tmp>" in message
    assert "<redacted-token>" in message
    assert "secret=<redacted>" in message
    assert "ghp_SecretToken12345" not in message
    assert "topSecret99" not in message
    assert "\x1b" not in message
    assert tmp_path.as_posix() not in message
    assert str(tmp_path) not in message
    if Path.home() != Path("/"):
        assert Path.home().as_posix() not in message
        assert str(Path.home()) not in message


def test_shell_bootstrap_diagnostic_stage_classification_and_stream_bounds(
    tmp_path: Path,
) -> None:
    assert _classify_bootstrap_stage([], log_exists=False) == "shell-startup"
    assert _classify_bootstrap_stage(["wrapper-start"], log_exists=False) == "wrapper-init"
    assert (
        _classify_bootstrap_stage(["wrapper-start", "before-source"], log_exists=False)
        == "installer-pre-download"
    )
    assert (
        _classify_bootstrap_stage(
            ["wrapper-start", "before-source", "download-enter:curl"], log_exists=False
        )
        == "download-log-write"
    )
    assert (
        _classify_bootstrap_stage(
            ["wrapper-start", "before-source", "download-enter:curl"], log_exists=True
        )
        == "download-destination-write"
    )
    assert (
        _classify_bootstrap_stage(
            [
                "wrapper-start",
                "before-source",
                "download-enter:curl",
                "download-wrote-destination",
            ],
            log_exists=True,
        )
        == "post-download"
    )

    rendered = _format_bootstrap_diagnostic(
        available="both",
        download_fails=True,
        executable="C:/Program Files/Git/bin/bash.exe",
        returncode=127,
        stdout="x" * 2000,
        stderr="Authorization: Bearer secret_abc123 C:\\Users\\alice\\private\\file.txt",
        stages=[],
        log_exists=False,
        tmp_path=tmp_path,
    )
    assert f"shell={sys.platform}:bash.exe" in rendered
    assert "C:/Program Files" not in rendered
    assert "stage=shell-startup" in rendered
    assert "secret_abc123" not in rendered
    assert "alice" not in rendered
    assert "<redacted>" in rendered
    assert "<user-home>" in rendered
    assert "x" * (_MAX_DIAGNOSTIC_STREAM_CHARS + 1) not in rendered
