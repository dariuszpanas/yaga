"""Launch the Rust ``yaga`` executable bundled with this Python wheel."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def find_yaga_bin() -> Path:
    """Return the path to the bundled ``yaga`` executable."""
    import yaga

    binary_name = "yaga.exe" if os.name == "nt" else "yaga"
    candidates: list[Path] = []
    for entry in getattr(yaga, "__path__", ()):
        candidates.append(Path(entry) / "_bin" / binary_name)
    candidates.append(Path(__file__).resolve().parent / "_bin" / binary_name)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "YAGA installation is missing its native executable. Reinstall yaga-cli."
    )


def _operation(args: list[str]) -> tuple[str | None, str | None]:
    words = iter(args)

    def next_word() -> str | None:
        word = next(words, None)
        return next(words, None) if word == "--" else word

    return next_word(), next_word()


def main(argv: list[str] | None = None) -> int:
    """Console entry point for the ``yaga`` script and ``python -m yaga``."""
    args = sys.argv[1:] if argv is None else list(argv)
    return _launch(args, console=argv is None)


def run_native(args: list[str]) -> int:
    """Invoke the bundled executable and return its exit code to the caller."""
    return _launch(list(args), console=False)


def _launch(args: list[str], *, console: bool) -> int:
    try:
        executable = find_yaga_bin()
    except FileNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2

    completion = os.environ.get("_YAGA_COMPLETE")
    if not completion and _operation(args) == ("self", "update"):
        # Let Clap validate argv first; exit 80/81 delegates to the Python manager.
        status = _call(executable, args, os.environ | {"_YAGA_WHEEL_UPDATE_REQUEST": "1"})
        if status in (80, 81):
            from yaga.self_update import run_wheel_update

            return run_wheel_update(dry_run=status == 80)
        return status

    if console and os.name == "posix":
        os.execve(executable, [str(executable), *args], os.environ)
    return _call(executable, args, dict(os.environ))


def _call(executable: Path, args: list[str], environment: dict[str, str]) -> int:
    try:
        code = subprocess.call([str(executable), *args], env=environment)
    except KeyboardInterrupt:
        return 130
    return 128 - code if code < 0 else code
