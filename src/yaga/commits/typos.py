"""Bounded optional integration with the Typos source-code spell checker."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from yaga.commits.models import Diagnostic
from yaga.errors import InputError, safe_error_text

MAX_OUTPUT_BYTES = 64 * 1024
TIMEOUT_SECONDS = 5


def check_typos(message: str, *, repository: Path | None = None) -> tuple[Diagnostic, ...]:
    """Check one commit message through an installed ``typos`` executable."""
    executable = shutil.which("typos")
    if executable is None:
        raise InputError("commit policy requires the typos executable, but it is not installed")
    try:
        completed = subprocess.run(
            [executable, "-", "--format", "json"],
            cwd=str(repository) if repository is not None else None,
            input=message.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InputError(
            "typos did not complete within its bounded commit-message check"
        ) from error
    if len(completed.stdout) > MAX_OUTPUT_BYTES or len(completed.stderr) > MAX_OUTPUT_BYTES:
        raise InputError("typos exceeded its bounded commit-message output limit")
    if completed.returncode == 0:
        if completed.stderr:
            raise InputError("typos returned unexpected diagnostic output on stderr")
        return ()
    if completed.returncode != 2:
        detail = safe_error_text(completed.stderr.decode("utf-8", errors="replace"))
        raise InputError(f"typos failed while checking the commit message: {detail}")
    return _parse_findings(completed.stdout)


def _parse_findings(output: bytes) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError("typos returned non-UTF-8 JSON output") from error
    for _line_number, line in enumerate(text.splitlines(), start=1):
        try:
            value: Any = json.loads(line)
        except json.JSONDecodeError as error:
            raise InputError("typos returned malformed JSON output") from error
        if not isinstance(value, dict) or value.get("type") != "typo":
            raise InputError("typos returned an unexpected JSON diagnostic")
        line_value = value.get("line_num")
        typo = value.get("typo")
        corrections = value.get("corrections")
        if (
            type(line_value) is not int
            or line_value < 1
            or line_value > 100_000
            or not isinstance(typo, str)
            or not typo
            or len(typo) > 128
            or not isinstance(corrections, list)
            or any(not isinstance(item, str) or not item for item in corrections)
        ):
            raise InputError("typos returned a malformed JSON diagnostic")
        suggestion = ", ".join(corrections[:8])
        message = f"possible typo {typo!r}"
        if suggestion:
            message += f"; suggestions: {suggestion}"
        diagnostics.append(Diagnostic(code="typos.word", message=message, line=line_value))
        if len(diagnostics) >= 256:
            raise InputError("typos returned too many commit-message diagnostics")
    if not diagnostics:
        raise InputError("typos reported findings without JSON diagnostics")
    return tuple(diagnostics)
