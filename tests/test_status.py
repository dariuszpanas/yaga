"""Tests for generic commit-status parsing."""

from __future__ import annotations

import pytest

from yaga.errors import GateError
from yaga.status import MAX_STATUS_DESCRIPTION_BYTES, parse_commit_status


def status_payload() -> dict[str, object]:
    return {
        "id": 123,
        "state": "pending",
        "context": "Codex Review",
        "description": "Waiting for exact-candidate evidence",
        "target_url": "https://github.com/owner/repository/actions/runs/123",
        "created_at": "2026-08-22T01:00:00Z",
        "creator": {"id": 41_898_282, "login": "github-actions[bot]"},
    }


def test_parse_commit_status_retains_security_relevant_fields() -> None:
    status = parse_commit_status(status_payload())

    assert status.status_id == 123
    assert status.state == "pending"
    assert status.context == "Codex Review"
    assert status.creator_id == 41_898_282
    assert status.creator_login == "github-actions[bot]"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", True, "bounded positive integer"),
        ("state", "queued", "state is invalid"),
        ("context", "", "bounded string"),
        ("description", "x" * (MAX_STATUS_DESCRIPTION_BYTES + 1), "oversized"),
        ("created_at", "not-a-time", "ISO-8601"),
    ],
)
def test_parse_commit_status_rejects_malformed_fields(
    field: str, value: object, message: str
) -> None:
    payload = status_payload()
    payload[field] = value
    with pytest.raises(GateError, match=message):
        parse_commit_status(payload)


def test_parse_commit_status_accepts_null_optional_text() -> None:
    payload = status_payload()
    payload["description"] = None
    payload["target_url"] = None

    status = parse_commit_status(payload)

    assert status.description is None
    assert status.target_url is None
