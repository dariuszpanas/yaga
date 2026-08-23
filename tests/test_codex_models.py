"""Tests for the Codex gate candidate handoff."""

from __future__ import annotations

import json

import pytest

from yaga.codex.models import MAX_CANDIDATE_BYTES, Candidate
from yaga.errors import GateError

HEAD = "a" * 40
BASE = "b" * 40


def candidate() -> Candidate:
    return Candidate(
        pull_request_number=7,
        head_sha=HEAD,
        base_sha=BASE,
        base_ref="main",
        head_repository="owner/repository",
        head_ref="feat/review-gate",
    )


def test_candidate_round_trip_uses_a_closed_compact_shape() -> None:
    encoded = candidate().to_json()

    assert Candidate.from_json(encoded) == candidate()
    assert encoded == (
        '{"base":"'
        + BASE
        + '","base_ref":"main","head":"'
        + HEAD
        + '","head_ref":"feat/review-gate",'
        '"head_repository":"owner/repository",'
        '"pr":7}'
    )


def test_candidate_rejects_unknown_fields_and_unbounded_input() -> None:
    payload = candidate().to_payload()
    payload["extra"] = "not permitted"
    with pytest.raises(GateError, match="fields"):
        Candidate.from_json(json.dumps(payload))

    with pytest.raises(GateError, match="invalid"):
        Candidate.from_json("x" * (MAX_CANDIDATE_BYTES + 1))

    with pytest.raises(GateError, match="invalid"):
        Candidate.from_json(None)


def test_direct_candidate_construction_is_also_validated() -> None:
    with pytest.raises(GateError, match="full lowercase"):
        Candidate(
            pull_request_number=7,
            head_sha="short",
            base_sha=BASE,
            base_ref="main",
            head_repository="owner/repository",
            head_ref="feature",
        )
