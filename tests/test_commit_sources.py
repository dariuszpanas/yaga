"""Tests for bounded, consistent explicit message sources."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from yaga.commits.checker import check_target
from yaga.commits.models import CommitPolicy
from yaga.commits.parser import MAX_MESSAGE_BYTES
from yaga.commits.sources import from_file, from_message, from_stdin
from yaga.errors import InputError


def test_utf8_bom_has_the_same_semantics_for_file_and_message_sources(tmp_path: Path) -> None:
    message = "\ufefffeat: add a command"
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_bytes(message.encode("utf-8"))

    file_target = from_file(path)
    direct_target = from_message(message)

    assert file_target.message == direct_target.message
    assert [
        diagnostic.code for diagnostic in check_target(file_target, CommitPolicy()).diagnostics
    ] == ["syntax.header"]
    assert [
        diagnostic.code for diagnostic in check_target(direct_target, CommitPolicy()).diagnostics
    ] == ["syntax.header"]


def test_direct_message_rejects_invalid_utf8_and_oversized_input() -> None:
    with pytest.raises(InputError, match="valid UTF-8"):
        from_message("\udcff")
    with pytest.raises(InputError, match="hard"):
        from_message("x" * (MAX_MESSAGE_BYTES + 1))


def test_file_source_rejects_invalid_utf8_and_oversized_input(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid-message"
    invalid.write_bytes(b"\xff")
    with pytest.raises(InputError, match="valid UTF-8"):
        from_file(invalid)

    oversized = tmp_path / "oversized-message"
    oversized.write_bytes(b"x" * (MAX_MESSAGE_BYTES + 1))
    with pytest.raises(InputError, match="hard"):
        from_file(oversized)


def test_stdin_source_enforces_the_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("é" * (MAX_MESSAGE_BYTES // 2 + 1)))

    with pytest.raises(InputError, match="hard"):
        from_stdin()


def test_text_stdin_fallback_converts_encoding_failures_to_input_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidTextInput(io.StringIO):
        @property
        def buffer(self) -> None:
            return None

        def read(self, _size: int | None = -1, /) -> str:
            return "\udcff"

    monkeypatch.setattr(sys, "stdin", InvalidTextInput())

    with pytest.raises(InputError, match="UTF-8"):
        from_stdin()
