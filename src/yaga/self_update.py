"""Explicit upgrades delegated to the manager of the running installation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from yaga.errors import ConfigurationError


@dataclass(frozen=True)
class UpdateResult:
    command: tuple[str, ...]
    log: Path | None = None


def update_command() -> tuple[str, ...]:
    """Resolve uv and prove that its named tool is the current environment."""
    prefix = Path(sys.prefix).resolve()
    if not (prefix / "uv-receipt.toml").is_file():
        if (prefix / "pipx_metadata.json").is_file():
            instruction = "run pipx upgrade yaga-cli"
        else:
            instruction = (
                "update yaga-cli with the manager of this environment "
                "(for example uv sync --upgrade-package yaga-cli or "
                "python -m pip install --upgrade yaga-cli)"
            )
        raise ConfigurationError(f"self update requires a uv tool installation; {instruction}")
    executable = shutil.which("uv")
    if executable is None or not Path(executable).is_absolute():
        raise ConfigurationError("uv is unavailable; restore uv on PATH and retry")
    uv = Path(executable).resolve()
    if uv.is_relative_to(Path.cwd().resolve()):
        raise ConfigurationError("refusing a uv executable from the current directory")
    try:
        result = subprocess.run(
            [str(uv), "--no-config", "tool", "dir"],
            cwd=prefix,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=True,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise ConfigurationError("could not establish ownership using uv tool dir") from error
    directory = result.stdout.strip()
    if not directory or len(directory) > 4096 or "\n" in directory:
        raise ConfigurationError("uv returned an invalid tool directory")
    root = Path(directory)
    if not root.is_absolute() or (root / "yaga-cli").resolve() != prefix:
        raise ConfigurationError("uv tool directory does not own this YAGA installation")
    return (str(uv), "--no-config", "tool", "upgrade", "yaga-cli")


def self_update(*, dry_run: bool = False) -> UpdateResult:
    """Upgrade only YAGA, retaining uv's stored version constraints and extras."""
    command = update_command()
    if not dry_run:
        environment = dict(os.environ)
        environment["UV_TOOL_DIR"] = str(Path(sys.prefix).resolve().parent)
        if sys.platform == "win32":
            return UpdateResult(command, _windows_handoff(command, environment))
        try:
            subprocess.run(
                command,
                cwd=Path(sys.prefix).resolve().parent,
                env=environment,
                timeout=600,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ConfigurationError(
                "uv could not complete the update; inspect its output and retry "
                "with uv tool upgrade yaga-cli"
            ) from error
    return UpdateResult(command)


def _windows_handoff(command: tuple[str, ...], environment: dict[str, str]) -> Path:
    """Run the manager only after the locked Windows launcher has exited."""
    if sys.platform != "win32":
        raise ConfigurationError("the Windows update handoff is unavailable on this platform")
    python = Path(sys.base_prefix) / "python.exe"
    if not python.is_file() or python.resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise ConfigurationError("cannot hand off this installation; run uv tool upgrade yaga-cli")
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
        raise ConfigurationError(
            "could not start the update; run uv tool upgrade yaga-cli"
        ) from error
    return log
