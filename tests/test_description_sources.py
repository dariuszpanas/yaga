"""Bounded local and GitHub-event input tests for PR descriptions."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from yaga.descriptions.sources import (
    load_description_event,
    read_description_file,
    read_description_stdin,
)
from yaga.errors import InputError


def _event(body: object = "Explain the change and its validation.") -> dict[str, object]:
    return {
        "action": "edited",
        "number": 23,
        "pull_request": {"number": 23, "state": "open", "draft": False, "body": body},
    }


def _write_event(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_description_file_preserves_utf8_and_source_lines(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    body = "\r\n## Résumé\r\n\r\nExplain the change.\n"
    path.write_bytes(body.encode("utf-8"))

    assert read_description_file(path) == body


@pytest.mark.parametrize(
    "raw",
    [b"\xff", b"x" * (65536 + 1), "é".encode() * 32769],
    ids=["invalid-utf8", "ascii-overflow", "unicode-byte-overflow"],
)
def test_description_file_rejects_bad_encoding_and_byte_overflow(
    tmp_path: Path, raw: bytes
) -> None:
    path = tmp_path / "body.md"
    path.write_bytes(raw)

    with pytest.raises(InputError):
        read_description_file(path)


def test_description_file_accepts_exact_hard_byte_boundary(tmp_path: Path) -> None:
    path = tmp_path / "body.md"
    body = "é" * 32768
    path.write_bytes(body.encode())

    assert read_description_file(path) == body


@pytest.mark.parametrize("source", ["missing", "directory"])
def test_description_file_requires_an_existing_regular_file(tmp_path: Path, source: str) -> None:
    path = tmp_path / source
    if source == "directory":
        path.mkdir()

    with pytest.raises(InputError):
        read_description_file(path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO boundary")
def test_description_file_rejects_fifo_without_opening_a_blocking_stream(tmp_path: Path) -> None:
    path = tmp_path / "body.fifo"
    make_fifo = getattr(os, "mkfifo", None)
    assert callable(make_fifo)
    make_fifo(path)

    with pytest.raises(InputError):
        read_description_file(path)


@pytest.mark.parametrize("binary", [False, True])
def test_description_stdin_preserves_lines(monkeypatch: pytest.MonkeyPatch, binary: bool) -> None:
    body = "\n## Summary\n\nExplain the change.\n"
    stream = io.TextIOWrapper(io.BytesIO(body.encode())) if binary else io.StringIO(body)
    monkeypatch.setattr(sys, "stdin", stream)

    assert read_description_stdin() == body


@pytest.mark.parametrize("binary", [False, True])
def test_description_stdin_rejects_utf8_byte_overflow(
    monkeypatch: pytest.MonkeyPatch, binary: bool
) -> None:
    body = "é" * 32769
    stream = io.TextIOWrapper(io.BytesIO(body.encode())) if binary else io.StringIO(body)
    monkeypatch.setattr(sys, "stdin", stream)

    with pytest.raises(InputError):
        read_description_stdin()


def test_description_stdin_rejects_invalid_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff")))

    with pytest.raises(InputError):
        read_description_stdin()


def test_event_adapter_needs_no_title_author_repository_or_git_fields(tmp_path: Path) -> None:
    body = "\n## Summary\n\nA literal $(command) remains description text.\n"
    event = load_description_event(_write_event(tmp_path, _event(body)))

    assert event.number == 23
    assert event.body == body


@pytest.mark.parametrize("body", [None, ""])
def test_event_empty_body_is_a_policy_input_not_a_source_failure(
    tmp_path: Path, body: object
) -> None:
    assert load_description_event(_write_event(tmp_path, _event(body))).body == ""


def test_event_missing_body_is_empty(tmp_path: Path) -> None:
    document = _event()
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    del pull_request["body"]

    assert load_description_event(_write_event(tmp_path, document)).body == ""


@pytest.mark.parametrize(
    ("action", "draft"),
    [
        ("opened", False),
        ("reopened", True),
        ("synchronize", False),
        ("edited", True),
        ("ready_for_review", False),
        ("converted_to_draft", True),
    ],
)
def test_event_supported_metadata_lifecycle_actions(
    tmp_path: Path, action: str, draft: bool
) -> None:
    document = _event()
    document["action"] = action
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["draft"] = draft

    assert load_description_event(_write_event(tmp_path, document)).number == 23


@pytest.mark.parametrize(
    ("scope", "key", "value"),
    [
        ("root", "action", "closed"),
        ("root", "action", "labeled"),
        ("root", "action", 1),
        ("root", "number", 24),
        ("root", "number", True),
        ("root", "number", 23.0),
        ("root", "number", 0),
        ("root", "number", 2**63),
        ("root", "pull_request", []),
        ("pull_request", "number", False),
        ("pull_request", "number", -1),
        ("pull_request", "state", "closed"),
        ("pull_request", "state", True),
        ("pull_request", "draft", 0),
        ("pull_request", "draft", "false"),
        ("pull_request", "body", True),
        ("pull_request", "body", 12),
        ("pull_request", "body", []),
        ("pull_request", "body", {}),
        ("pull_request", "body", "\ud800"),
        ("pull_request", "body", "é" * 32769),
    ],
    ids=[
        "closed-action",
        "unknown-action",
        "action-type",
        "number-mismatch",
        "number-bool",
        "number-float",
        "number-zero",
        "number-overflow",
        "pull-request-type",
        "nested-bool",
        "nested-negative",
        "closed-state",
        "state-type",
        "draft-int",
        "draft-string",
        "body-bool",
        "body-int",
        "body-list",
        "body-object",
        "body-surrogate",
        "body-overflow",
    ],
)
def test_event_rejects_invalid_selected_fields(
    tmp_path: Path, scope: str, key: str, value: object
) -> None:
    document = _event()
    target = document if scope == "root" else document["pull_request"]
    assert isinstance(target, dict)
    target[key] = value

    with pytest.raises(InputError):
        load_description_event(_write_event(tmp_path, document))


@pytest.mark.parametrize(
    ("action", "draft"), [("ready_for_review", True), ("converted_to_draft", False)]
)
def test_event_rejects_inconsistent_draft_transitions(
    tmp_path: Path, action: str, draft: bool
) -> None:
    document = _event()
    document["action"] = action
    pull_request = document["pull_request"]
    assert isinstance(pull_request, dict)
    pull_request["draft"] = draft

    with pytest.raises(InputError):
        load_description_event(_write_event(tmp_path, document))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b"{} garbage",
        b"[]",
        b"null",
        b'{"number":23,"number":23}',
        b'{"ignored":{"value":1,"value":2}}',
        b'{"ignored":NaN}',
        b'{"ignored":Infinity}',
        b'{"ignored":-Infinity}',
        b'{"ignored":1e999}',
        b"[" * 2000 + b"0" + b"]" * 2000,
        b" " * (1024 * 1024 + 1),
    ],
    ids=[
        "empty",
        "invalid-utf8",
        "trailing-garbage",
        "array",
        "null",
        "duplicate-root-key",
        "duplicate-nested-key",
        "nan",
        "infinity",
        "negative-infinity",
        "float-overflow",
        "deep-json",
        "byte-overflow",
    ],
)
def test_event_rejects_ambiguous_malformed_or_over_budget_json(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "event.json"
    path.write_bytes(raw)

    with pytest.raises(InputError):
        load_description_event(path)


@pytest.mark.parametrize(
    "metadata", ['{"value":1,"value":2}', "NaN", "Infinity", "-Infinity", "1e999"]
)
def test_event_rejects_ambiguous_ignored_metadata_in_otherwise_valid_event(
    tmp_path: Path, metadata: str
) -> None:
    raw = json.dumps(_event())[:-1] + ',"ignored":' + metadata + "}"
    path = tmp_path / "event.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(InputError):
        load_description_event(path)


def test_event_requires_regular_file(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        load_description_event(tmp_path)
