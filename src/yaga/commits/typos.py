"""Bounded optional integration with the Typos source-code spell checker."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from yaga.commits.models import CheckResult, CommitPolicy, Diagnostic, TyposPolicy
from yaga.commits.parser import MAX_MESSAGE_BYTES, normalize_message
from yaga.errors import InputError, safe_error_text
from yaga.git.runtime import run_bounded_process

MAX_OUTPUT_BYTES = 64 * 1024
TIMEOUT_SECONDS = 5
MAX_CORRECTIONS = 16
MAX_CORRECTION_BYTES = 128
MAX_SUGGESTION_BYTES = 256
MAX_BYTE_OFFSET = 100_000


def apply_typos(
    result: CheckResult, policy: CommitPolicy, *, repository: Path, isolated: bool = False
) -> CheckResult:
    """Apply optional spelling checks after structural checks and skip decisions."""
    if policy.typos is not TyposPolicy.CHECK or result.skipped_reason is not None:
        return result
    diagnostics = check_typos(result.target.message, repository=repository, isolated=isolated)
    return replace(result, diagnostics=(*result.diagnostics, *diagnostics))


def check_typos(
    message: str, *, repository: Path | None = None, isolated: bool = False
) -> tuple[Diagnostic, ...]:
    """Check one commit message through an installed ``typos`` executable."""
    executable = shutil.which("typos")
    if executable is None:
        raise InputError("commit policy requires the typos executable, but it is not installed")
    if isolated and repository is not None:
        try:
            resolved = Path(executable).resolve(strict=True)
            inside_repository = resolved.is_relative_to(repository.resolve())
        except (OSError, RuntimeError) as error:
            raise InputError("trusted typos executable could not be resolved") from error
        if inside_repository:
            raise InputError(
                "trusted spelling checks require typos installed outside the repository"
            )
        executable = str(resolved)
    try:
        message_bytes = normalize_message(message).encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError("commit message is not valid UTF-8") from error
    if len(message_bytes) > MAX_MESSAGE_BYTES:
        raise InputError("commit message exceeds the hard byte limit for Typos input")
    try:
        completed = run_bounded_process(
            [executable, "-", "--format", "json", *(["--isolated"] if isolated else [])],
            cwd=repository,
            stdin_data=message_bytes,
            stdout_limit=MAX_OUTPUT_BYTES,
            stderr_limit=MAX_OUTPUT_BYTES,
            timeout_seconds=TIMEOUT_SECONDS,
        )
    except (OSError, ValueError) as error:
        raise InputError("typos could not complete its bounded commit-message check") from error
    if completed.timed_out:
        raise InputError("typos did not complete within its bounded commit-message check")
    if completed.stdout_overflow or completed.stderr_overflow:
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
        except (ValueError, RecursionError) as error:
            raise InputError("typos returned malformed JSON output") from error
        if not isinstance(value, dict) or value.get("type") != "typo":
            raise InputError("typos returned an unexpected JSON diagnostic")
        line_value = value.get("line_num")
        byte_offset = value.get("byte_offset")
        typo = value.get("typo")
        corrections = value.get("corrections")
        if (
            type(line_value) is not int
            or line_value < 1
            or line_value > 100_000
            or (
                byte_offset is not None
                and (
                    type(byte_offset) is not int or byte_offset < 0 or byte_offset > MAX_BYTE_OFFSET
                )
            )
            or not isinstance(typo, str)
            or not typo
            or _utf8_length(typo) > MAX_CORRECTION_BYTES
            or not isinstance(corrections, list)
            or len(corrections) > MAX_CORRECTIONS
            or any(not isinstance(item, str) or not item for item in corrections)
            or any(_utf8_length(item) > MAX_CORRECTION_BYTES for item in corrections)
        ):
            raise InputError("typos returned a malformed JSON diagnostic")
        suggestion = _bounded_text(", ".join(corrections[:8]), MAX_SUGGESTION_BYTES)
        message = f"possible typo {typo!r}"
        if suggestion:
            message += f"; suggestions: {suggestion}"
        diagnostics.append(
            Diagnostic(
                code="typos.word",
                message=message,
                line=line_value,
                column=byte_offset + 1 if byte_offset is not None else 1,
            )
        )
        if len(diagnostics) >= 256:
            raise InputError("typos returned too many commit-message diagnostics")
    if not diagnostics:
        raise InputError("typos reported findings without JSON diagnostics")
    return tuple(diagnostics)


def _utf8_length(value: str) -> int:
    """Reject JSON's escaped surrogate strings at the diagnostic boundary."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("typos returned a malformed JSON diagnostic") from error


def _bounded_text(value: str, maximum_bytes: int) -> str:
    """Keep a diagnostic field within a UTF-8 byte bound without splitting text."""
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore")
