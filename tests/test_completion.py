"""Tests for BOM-preserving, ExecutionPolicy-safe shell completion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import shellingham
from typer.testing import CliRunner

from yaga import completion
from yaga.cli import app
from yaga.errors import InputError

runner = CliRunner()


@pytest.mark.parametrize(
    ("bom", "codec", "newline"),
    [
        (b"\xff\xfe", "utf-16-le", "\r\n"),
        (b"\xfe\xff", "utf-16-be", "\r\n"),
        (b"\xef\xbb\xbf", "utf-8", "\r\n"),
        (b"", "utf-8", "\n"),
    ],
)
def test_install_powershell_preserves_existing_profile_bom_encoding_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bom: bytes,
    codec: str,
    newline: str,
) -> None:
    profile = tmp_path / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
    profile.parent.mkdir(parents=True)
    header = f"Set-Alias ll Get-ChildItem{newline}# User profile customization{newline}"
    profile.write_bytes(bom + header.encode(codec))

    commands_executed: list[list[str]] = []

    def fake_which(shell: str) -> str:
        return f"/fake/bin/{shell}"

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands_executed.append(list(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{profile}\r\n".encode(),
            stderr=b"",
        )

    monkeypatch.setattr(completion.shutil, "which", fake_which)
    monkeypatch.setattr(completion.subprocess, "run", fake_run)
    monkeypatch.setattr(
        completion.shellingham,
        "detect_shell",
        lambda: ("powershell.exe", "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
    )

    first = runner.invoke(app, ["--install-completion"])
    assert first.exit_code == 0, first.output
    assert "powershell completion installed in" in first.stdout

    # Never invoke Set-ExecutionPolicy!
    assert len(commands_executed) == 1
    assert all("Set-ExecutionPolicy" not in " ".join(cmd) for cmd in commands_executed)
    assert "-NoProfile" in commands_executed[0]

    raw_first = profile.read_bytes()
    assert raw_first.startswith(bom)
    decoded_first = raw_first[len(bom) :].decode(codec)
    assert "Set-Alias ll Get-ChildItem" in decoded_first
    assert "Register-ArgumentCompleter -Native -CommandName yaga" in decoded_first
    assert "Set-PSReadLineKeyHandler" not in decoded_first
    assert decoded_first.count(completion.MARKER_START) == 1
    assert decoded_first.count(completion.MARKER_END) == 1
    # Ensure zero CJK mojibake from UTF-8-into-UTF-16 corruption
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in decoded_first)

    # Re-running updates in place without duplicating the block
    second = runner.invoke(app, ["--install-completion"])
    assert second.exit_code == 0, second.output
    assert profile.read_bytes() == raw_first


def test_install_powershell_creates_utf8_bom_profile_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "Profile" / "Microsoft.PowerShell_profile.ps1"
    monkeypatch.setattr(completion.shutil, "which", lambda shell: f"/fake/bin/{shell}")
    monkeypatch.setattr(
        completion.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(
            cmd, 0, stdout=f"{profile}\n".encode(), stderr=b""
        ),
    )

    shell, installed = completion.install_completion("powershell")
    assert shell == "powershell"
    assert installed == profile
    raw = profile.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw[3:].decode("utf-8")
    assert "Register-ArgumentCompleter -Native -CommandName yaga" in text


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_install_posix_shells_writes_isolated_completion_files_idempotently(
    tmp_path: Path,
    shell: str,
) -> None:
    resolved_shell, path = completion.install_completion(shell, home=tmp_path)
    assert resolved_shell == shell
    assert path.is_file()
    first_content = path.read_text(encoding="utf-8")
    assert "yaga" in first_content

    completion.install_completion(shell, home=tmp_path)
    if shell == "bash":
        bashrc = (tmp_path / ".bashrc").read_text(encoding="utf-8")
        assert bashrc.count(completion.MARKER_START) == 1
    elif shell == "zsh":
        zshrc = (tmp_path / ".zshrc").read_text(encoding="utf-8")
        assert zshrc.count(completion.MARKER_START) == 1


def test_refuses_corrupt_or_bomless_nul_profile_fail_closed(tmp_path: Path) -> None:
    profile = tmp_path / "profile.ps1"
    profile.write_bytes(b"I\x00m\x00p\x00o\x00r\x00t\x00")
    original = profile.read_bytes()

    with pytest.raises(InputError, match="NUL bytes without a UTF-16 BOM"):
        completion._upsert_marked_block(
            profile,
            "Register-ArgumentCompleter",
            default_newline="\r\n",
            default_bom=b"",
        )
    assert profile.read_bytes() == original


def test_show_completion_and_runtime_powershell_completion_work_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        completion.shellingham,
        "detect_shell",
        lambda: ("pwsh", "/usr/bin/pwsh"),
    )
    shown = runner.invoke(app, ["--show-completion"])
    assert shown.exit_code == 0
    assert "Register-ArgumentCompleter -Native -CommandName yaga" in shown.stdout
    assert "Set-PSReadLineKeyHandler" not in shown.stdout

    completed = runner.invoke(
        app,
        [],
        env={
            "_YAGA_COMPLETE": "complete_powershell",
            "_TYPER_COMPLETE_ARGS": "yaga co",
            "_TYPER_COMPLETE_WORD_TO_COMPLETE": "co",
        },
    )
    assert completed.exit_code == 0
    lines = completed.stdout.splitlines()
    assert any(line.startswith("commit:::") for line in lines)
    assert any(line.startswith("config:::") for line in lines)


def test_undetected_shell_reports_sanitized_exit_two(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_detect() -> tuple[str, str]:
        raise shellingham.ShellDetectionFailure()

    monkeypatch.setattr(completion.shellingham, "detect_shell", fail_detect)
    monkeypatch.setenv("SHELL", "")
    result = runner.invoke(app, ["--show-completion"])
    assert result.exit_code == 2
    assert "YAGA input error: could not detect the current shell" in result.stderr
    assert "Traceback" not in result.stderr
