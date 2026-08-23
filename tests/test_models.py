"""Tests for shared strict payload primitives."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from yaga.errors import GateError
from yaga.models import (
    MAX_EVENT_BYTES,
    MAX_GITHUB_ID,
    MAX_REF_BYTES,
    actor_is,
    commit_sha,
    positive_int,
    read_json_object,
    ref_name,
    repository_name,
    request_timeout,
    timestamp,
    workflow_path,
)


@pytest.mark.parametrize("value", [True, 0, -1, MAX_GITHUB_ID + 1, "1", None])
def test_positive_int_rejects_non_ids(value: object) -> None:
    with pytest.raises(GateError, match="bounded positive integer"):
        positive_int(value, "test ID")


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "/repo",
        "owner/",
        "owner/repo/more",
        "owner name/repo",
        "./repo",
        "owner/..",
        42,
    ],
)
def test_repository_name_requires_one_ascii_owner_and_name(value: object) -> None:
    with pytest.raises(GateError, match="owner/name|unsafe path"):
        repository_name(value)


@pytest.mark.parametrize("value", ["A" * 40, "a" * 39, "g" * 40, 42])
def test_commit_sha_requires_full_lowercase_sha(value: object) -> None:
    with pytest.raises(GateError, match="full lowercase"):
        commit_sha(value, "head")


@pytest.mark.parametrize("value", ["", " main", "main\n", "x" * (MAX_REF_BYTES + 1)])
def test_ref_name_is_nonempty_safe_and_bounded(value: object) -> None:
    with pytest.raises(GateError):
        ref_name(value)


def test_timestamp_is_timezone_aware_and_normalized() -> None:
    assert timestamp("2026-08-22T01:02:03-07:00", "created") == datetime(
        2026, 8, 22, 8, 2, 3, tzinfo=UTC
    )
    for value in (None, "2026-08-22T01:02:03", "not-a-time"):
        with pytest.raises(GateError):
            timestamp(value, "created")


def test_actor_match_requires_both_id_and_login() -> None:
    actor = {"user": {"id": 123, "login": "trusted[bot]"}}

    assert actor_is(actor, user_id=123, user_login="trusted[bot]")
    assert not actor_is(actor, user_id=124, user_login="trusted[bot]")
    assert not actor_is(actor, user_id=123, user_login="lookalike[bot]")
    assert not actor_is({"user": "invalid"}, user_id=123, user_login="trusted[bot]")
    assert not actor_is(
        {"user": {"id": True, "login": "trusted[bot]"}},
        user_id=1,
        user_login="trusted[bot]",
    )
    assert not actor_is(
        {"user": {"id": 1, "login": "trusted[bot]"}},
        user_id=True,
        user_login="trusted[bot]",
    )


@pytest.mark.parametrize(
    "value",
    ["codex.yml", ".github/workflows/subdir/codex.yml", ".github/workflows/codex.yamlx"],
)
def test_workflow_path_is_repository_relative_and_direct(value: object) -> None:
    with pytest.raises(GateError, match="repository workflow path"):
        workflow_path(value, "publisher workflow")


@pytest.mark.parametrize("value", [True, 0, 31, float("inf"), "not-a-number"])
def test_request_timeout_is_numeric_finite_and_bounded(value: object) -> None:
    with pytest.raises(GateError):
        request_timeout(value)


def test_read_json_object_is_bounded_and_requires_an_object(tmp_path) -> None:  # type: ignore[no-untyped-def]
    valid = tmp_path / "event.json"
    valid.write_text(json.dumps({"action": "opened"}), encoding="utf-8")
    assert read_json_object(str(valid), label="event") == {"action": "opened"}

    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(GateError, match="must be an object"):
        read_json_object(str(array), label="event")

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_EVENT_BYTES + 1))
    with pytest.raises(GateError, match="too large"):
        read_json_object(str(oversized), label="event")
