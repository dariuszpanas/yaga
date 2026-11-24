"""Tests for the zero-dependency Python wheel launcher and self-update handoff."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import yaga
from scripts import native_wheel
from yaga import entrypoint, self_update


def test_package_exports_find_yaga_bin() -> None:
    assert yaga.__all__ == ["find_yaga_bin"]
    assert yaga.find_yaga_bin is entrypoint.find_yaga_bin


def test_find_yaga_bin_discovers_bundled_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary_name = "yaga.exe" if os.name == "nt" else "yaga"
    bundled = tmp_path / "_bin" / binary_name
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"binary")
    monkeypatch.setattr(yaga, "__path__", [str(tmp_path)])
    assert entrypoint.find_yaga_bin() == bundled


def test_find_yaga_bin_raises_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yaga, "__path__", [str(tmp_path)])
    monkeypatch.setattr(entrypoint, "__file__", str(tmp_path / "entrypoint.py"))
    with pytest.raises(FileNotFoundError, match="missing its native executable"):
        entrypoint.find_yaga_bin()


def test_main_returns_exit_2_when_binary_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(yaga, "__path__", [str(tmp_path)])
    monkeypatch.setattr(entrypoint, "__file__", str(tmp_path / "entrypoint.py"))
    assert entrypoint.main(["--version"]) == 2
    captured = capsys.readouterr()
    assert "missing its native executable" in captured.err


def test_run_native_delegates_arguments_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / ("yaga.exe" if os.name == "nt" else "yaga")
    binary.write_bytes(b"stub")
    monkeypatch.setattr(entrypoint, "find_yaga_bin", lambda: binary)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_call(cmd: list[str], *, env: dict[str, str]) -> int:
        calls.append((cmd, env))
        return 1

    monkeypatch.setattr(entrypoint.subprocess, "call", fake_call)
    assert entrypoint.run_native(["commit", "check", "--message", "bad"]) == 1
    assert calls[0][0] == [str(binary), "commit", "check", "--message", "bad"]


def test_self_update_delegates_on_wheel_update_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / ("yaga.exe" if os.name == "nt" else "yaga")
    binary.write_bytes(b"stub")
    monkeypatch.setattr(entrypoint, "find_yaga_bin", lambda: binary)
    monkeypatch.delenv("_YAGA_COMPLETE", raising=False)
    monkeypatch.setattr(entrypoint.subprocess, "call", lambda cmd, env: 80)

    seen: list[bool] = []
    monkeypatch.setattr(
        self_update,
        "run_wheel_update",
        lambda *, dry_run: (seen.append(dry_run), 0)[1],
    )
    assert entrypoint.run_native(["self", "update", "--dry-run"]) == 0
    assert seen == [True]


def test_self_update_reports_unmanaged_and_pipx_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    with pytest.raises(self_update.UpdateError, match="uv sync --upgrade-package yaga-cli"):
        self_update.self_update(dry_run=True)

    (tmp_path / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    with pytest.raises(self_update.UpdateError, match="pipx upgrade yaga-cli"):
        self_update.self_update(dry_run=True)


def test_self_update_verifies_uv_tool_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools_dir = tmp_path / "tools"
    env_dir = tools_dir / "yaga-cli"
    env_dir.mkdir(parents=True)
    (env_dir / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")

    uv_bin = tmp_path / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    uv_bin.parent.mkdir(parents=True)
    uv_bin.write_bytes(b"uv")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    monkeypatch.setattr(sys, "prefix", str(env_dir))
    monkeypatch.chdir(work_dir)
    monkeypatch.setattr(self_update.shutil, "which", lambda name: str(uv_bin))
    monkeypatch.setattr(self_update, "_probe", lambda cmd, cwd, timeout: f"{tools_dir}\n")

    result = self_update.self_update(dry_run=True)
    assert result.command == (
        str(uv_bin.resolve()),
        "--no-config",
        "tool",
        "upgrade",
        "yaga-cli",
    )
    assert result.log is None


def test_native_wheel_hook_honors_platform_override_for_source_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = tmp_path / "yaga"
    built.write_bytes(b"binary")
    cargo_json = json.dumps(
        {
            "reason": "compiler-artifact",
            "target": {"name": "yaga"},
            "executable": str(built),
        }
    )

    monkeypatch.delenv("YAGA_PREBUILT_BINARY", raising=False)
    monkeypatch.setenv("YAGA_CARGO_TARGET", "x86_64-unknown-linux-musl")
    monkeypatch.setenv("YAGA_WHEEL_PLATFORM", "manylinux_2_28_x86_64")
    monkeypatch.setattr(native_wheel.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        native_wheel.subprocess,
        "check_output",
        lambda *args, **kwargs: "x86_64-unknown-linux-gnu\n",
    )

    seen_cmd: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{cargo_json}\n", stderr="")

    monkeypatch.setattr(native_wheel.subprocess, "run", fake_run)

    hook = object.__new__(native_wheel.NativeWheelHook)
    monkeypatch.setattr(native_wheel.NativeWheelHook, "root", property(lambda self: str(tmp_path)))
    build_data: dict[str, Any] = {"force_include": {}}
    hook.initialize("standard", build_data)

    assert "--target" in seen_cmd and "x86_64-unknown-linux-musl" in seen_cmd
    assert build_data["tag"] == "py3-none-manylinux_2_28_x86_64"
    assert build_data["pure_python"] is False
    assert build_data["force_include"] == {str(built): "yaga/_bin/yaga"}
