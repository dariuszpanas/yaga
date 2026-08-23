"""Tests for bounded, exact Codex connector evidence."""

from __future__ import annotations

import pytest

from tests.codex_support import (
    CONNECTOR_USER,
    HEAD,
    FakeApi,
    _clean_body,
    _clean_comment,
    _clean_footer,
    _formal_body,
    _publish,
)
from yaga.codex import constants, evidence


@pytest.mark.parametrize(
    "suffix",
    [
        ":+1:",
        ":rocket:",
        ":tada:",
        "Already looking forward to the next diff.",
        "Another round soon, please!",
        "Bravo.",
        "Breezy!",
        "Can't wait for the next one!",
        "Chef's kiss.",
        "Delightful!",
        "Hooray!",
        "Keep it up!",
        "Keep them coming!",
        "More of your lovely PRs please.",
        "Nice work!",
        "Swish!",
        "What shall we delve into next?",
        "You're on a roll.",
        None,
        "Novel but authenticated ✨",
    ],
)
def test_clean_outcome_accepts_bounded_optional_single_line_flourishes(
    suffix: str | None,
) -> None:
    assert evidence._clean_reviewed_commit(_clean_body(suffix=suffix)) == HEAD


@pytest.mark.parametrize(
    "body",
    [
        "@codex review",
        f"Codex Review: Didn't find any major issues. {'x' * 81}\n\n**Reviewed commit:** `{HEAD}`",
        f"Codex Review: Didn't find any major issues. {'✨' * 27}\n\n**Reviewed commit:** `{HEAD}`",
        f"Codex Review: Didn't find any major issues. Bravo.\nextra\n\n"
        f"**Reviewed commit:** `{HEAD}`",
        f"Codex Review: Didn't find any major issues. Bravo.\t\n\n**Reviewed commit:** `{HEAD}`",
        f"Codex Review: Didn't find any major issues. Bravo.\u0085Again\n\n"
        f"**Reviewed commit:** `{HEAD}`",
        f"Codex Review: Didn't find any major issues. Bravo.\u2028Again\n\n"
        f"**Reviewed commit:** `{HEAD}`",
        _clean_body() + f"\n**Reviewed commit:** `{HEAD}`",
        _clean_body() + "\nActually found a blocker.",
        "prefix\n" + _clean_body(),
    ],
)
def test_clean_outcome_rejects_requests_spoofs_and_ambiguous_markers(body: str) -> None:
    assert evidence._clean_reviewed_commit(body) is None


def test_clean_outcome_normalizes_crlf_but_requires_one_marker() -> None:
    assert evidence._clean_reviewed_commit(_clean_body().replace("\n", "\r\n")) == HEAD


def test_connector_body_with_a_lone_surrogate_is_not_evidence() -> None:
    assert evidence._clean_reviewed_commit("\ud800") is None


@pytest.mark.parametrize("current", [True, False])
def test_clean_outcome_accepts_observed_connector_details_footer(current: bool) -> None:
    assert evidence._clean_reviewed_commit(_clean_body() + _clean_footer(current=current)) == HEAD


def test_clean_outcome_rejects_unrecognized_details_footer() -> None:
    assert evidence._clean_reviewed_commit(_clean_body() + "\n\n<details>other</details>") is None


def test_formal_outcome_requires_exact_prefix_and_exactly_one_marker() -> None:
    assert evidence._formal_reviewed_commit(_formal_body()) == HEAD
    assert evidence._formal_reviewed_commit("note\n" + _formal_body()) is None
    assert evidence._formal_reviewed_commit(_formal_body() + f"\n{_formal_body()}") is None
    assert (
        evidence._formal_reviewed_commit(_formal_body().replace("Codex Review", "Review")) is None
    )


def test_formal_outcome_accepts_observed_leading_newline_and_details_footer() -> None:
    assert evidence._formal_reviewed_commit("\n" + _formal_body() + _clean_footer()) == HEAD


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (CONNECTOR_USER, True),
        (
            {
                "id": constants.CODEX_CONNECTOR_USER_ID + 1,
                "login": constants.CODEX_CONNECTOR_LOGIN,
            },
            False,
        ),
        ({"id": constants.CODEX_CONNECTOR_USER_ID, "login": "codex-lookalike[bot]"}, False),
    ],
)
def test_clean_policy_requires_exact_connector_id_and_login(
    user: dict[str, object], expected: bool
) -> None:
    api = FakeApi(comments=[_clean_comment(user=user)])
    if expected:
        assert "clean comment" in _publish(api)
        assert [payload["state"] for _, payload in api.posts] == ["success"]
    else:
        assert _publish(api).startswith("pending for ")
        assert api.posts == []


@pytest.mark.parametrize(
    "app",
    [
        None,
        {
            "id": constants.CODEX_CONNECTOR_APP_ID + 1,
            "slug": constants.CODEX_CONNECTOR_APP_SLUG,
        },
        {"id": constants.CODEX_CONNECTOR_APP_ID, "slug": "codex-lookalike"},
    ],
)
def test_clean_policy_requires_exact_connector_app(app: object) -> None:
    api = FakeApi(comments=[_clean_comment(app=app)])

    assert _publish(api).startswith("pending for ")
    assert api.posts == []
