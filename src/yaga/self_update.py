"""Explicit upgrades delegated to the Python tool manager owning this installation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_UNMANAGED = (
    "self update requires a uv tool installation; update yaga-cli with the manager "
    "of this environment (for example uv sync --upgrade-package yaga-cli or "
    "python -m pip install --upgrade yaga-cli)"
)
_PIPX = "self update requires a uv tool installation; run pipx upgrade yaga-cli"


class UpdateError(RuntimeError):
    """Raised when the running Python environment cannot be updated safely."""


@dataclass(frozen=True)
class UpdateResult:
    command: tuple[str, ...]
    log: Path | None = None


def _safe_text(value: object, *, maximum: int = 1000) -> str:
    text = "".join(
        ch if ch.isprintable() and ch not in "\r\n\t" else " " for ch in str(value)
    ).strip()
    if len(text) > maximum:
        return text[: maximum - 3] + "..."
    return text


def _configure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def update_command() -> tuple[str, ...]:
    """Resolve uv and prove that its named tool is the current environment."""
    return self_update(dry_run=True).command


def self_update(*, dry_run: bool = False) -> UpdateResult:
    """Upgrade only YAGA, retaining uv's stored version constraints and extras."""
    prefix = Path(sys.prefix).resolve()
    if not (prefix / "uv-receipt.toml").is_file():
        if (prefix / "pipx_metadata.json").is_file():
            raise UpdateError(_PIPX)
        raise UpdateError(_UNMANAGED)

    raw_uv = shutil.which("uv")
    if not raw_uv:
        raise UpdateError("uv is unavailable; restore uv on PATH and retry")
    uv_path = Path(raw_uv)
    if not uv_path.is_absolute():
        raise UpdateError("uv is unavailable; restore uv on PATH and retry")
    uv_resolved = uv_path.resolve()
    try:
        cwd = Path.cwd().resolve()
    except OSError as error:
        raise UpdateError("could not resolve current working directory") from error
    if uv_resolved.is_relative_to(cwd):
        raise UpdateError("refusing a uv executable from the current directory")

    probe_cmd = (str(uv_resolved), "--no-config", "tool", "dir")
    directory = _probe(probe_cmd, prefix, timeout=15).strip(" \t\r\n\x0b\x0c\x1c\x1d\x1e\x1f")
    if not directory or len(directory) > 4096 or "\n" in directory:
        raise UpdateError("uv returned an invalid tool directory")
    root = Path(directory)
    if not root.is_absolute() or (root / "yaga-cli").resolve() != prefix:
        raise UpdateError("uv tool directory does not own this YAGA installation")

    command = (str(uv_resolved), "--no-config", "tool", "upgrade", "yaga-cli")
    log = None if dry_run else _upgrade(command, prefix.parent, timeout=600)
    return UpdateResult(command, log)


def _probe(command: tuple[str, ...], cwd: Path, *, timeout: int) -> str:
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        result.stderr.decode("utf-8")
        return result.stdout.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise UpdateError("could not establish ownership using uv tool dir") from error


def _upgrade(command: tuple[str, ...], root: Path, *, timeout: int) -> Path | None:
    environment = dict(os.environ)
    environment["UV_TOOL_DIR"] = str(root)
    if sys.platform == "win32":
        return _windows_handoff(command, environment)
    try:
        subprocess.run(command, cwd=root, env=environment, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise UpdateError(
            "uv could not complete the update; inspect its output and retry "
            "with uv tool upgrade yaga-cli"
        ) from error
    return None


def _windows_handoff(command: tuple[str, ...], environment: dict[str, str]) -> Path:
    """Run the manager only after the locked Windows launcher has exited."""
    if sys.platform != "win32":
        raise UpdateError("the Windows update handoff is unavailable on this platform")
    python = Path(sys.base_prefix) / "python.exe"
    if not python.is_file() or python.resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise UpdateError("cannot hand off this installation; run uv tool upgrade yaga-cli")
    directory = Path(tempfile.mkdtemp(prefix="yaga-update-"))
    worker = directory / "worker.py"
    log = directory / "update.log"
    try:
        worker.write_bytes(Path(__file__).with_name("_update_worker.py").read_bytes())
        with log.open("wb") as output:
            subprocess.Popen(
                [str(python), "-I", str(worker), command[0], str(os.getpid()), str(os.getppid())],
                cwd=Path(sys.prefix).resolve().parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    except OSError as error:
        worker.unlink(missing_ok=True)
        raise UpdateError("could not start the update; run uv tool upgrade yaga-cli") from error
    return log


def run_wheel_update(*, dry_run: bool) -> int:
    """Run a self-update request parsed by the bundled CLI."""
    _configure_utf8_output()
    try:
        result = self_update(dry_run=dry_run)
    except KeyboardInterrupt:
        return 130
    except UpdateError as error:
        print(_safe_text(error), file=sys.stderr)
        return 2
    report_update(result, dry_run, print)
    return 0


def report_update(result: UpdateResult, dry_run: bool, emit: Callable[[str], object]) -> None:
    if dry_run:
        emit("Update command: " + _safe_text(repr(list(result.command))))
    elif result.log is not None:
        emit("Update scheduled after YAGA exits. Completion log: " + _safe_text(result.log))
        emit("Wait for YAGA_UPDATE_EXIT_CODE=0 in that log, then run yaga --version.")
        path = result.log.as_posix()
        emit("Read the log (repeat if the completion code is not present yet):")
        emit("  Bash/zsh: " + _safe_text("cat -- " + shlex.quote(path)))
        emit(
            "  PowerShell: "
            + _safe_text("Get-Content -LiteralPath '" + str(result.log).replace("'", "''") + "'")
        )
        emit("For future updates with foreground progress, run uv tool upgrade yaga-cli directly.")
    else:
        emit("uv completed the update. Run yaga --version to inspect the installed version.")
