"""Parse commit-bound Codex connector outcomes without writing repository state."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from yaga.codex.constants import (
    CODEX_CONNECTOR_APP_ID,
    CODEX_CONNECTOR_APP_SLUG,
    CODEX_CONNECTOR_LOGIN,
    CODEX_CONNECTOR_USER_ID,
    FORMAL_REVIEW_PREFIX,
    MAX_OUTCOME_BODY_BYTES,
    REACTION_CONTENTS,
    REVIEW_STATES,
)
from yaga.errors import GateError
from yaga.github import RestApi
from yaga.models import PullRequest, actor_is, commit_sha, positive_int, record, timestamp

CLEAN_OUTCOME_RE = re.compile(
    r"\ACodex Review: Didn't find any major issues\."
    r"(?: (?P<flourish>[^\r\n]{1,80}))?\n\n"
    r"\*\*Reviewed commit:\*\* `(?P<commit>[0-9a-f]{10,40})`(?P<footer>[\s\S]*)\Z"
)
CLEAN_OUTCOME_FOOTER_RE = re.compile(
    r"\n\n<details> <summary>ℹ️ About Codex in GitHub</summary>\n<br/>\n\n"
    r"(?:"
    r"\[Your team has set up Codex to review pull requests in this repo\]"
    r"\(https://chatgpt\.com/codex/cloud/settings/general\)\. Reviews are triggered when you\n"
    r"|"
    r"Codex has been enabled to automatically review pull requests in this repo\. "
    r"Reviews are triggered when you\n"
    r")"
    r"- Open a pull request for review\n"
    r"- Mark a draft as ready\n"
    r'- Comment "@codex review"\.\n\n'
    r"If Codex has suggestions, it will comment; otherwise it will react with 👍\.\n"
    r"\s*"
    r"(?:"
    r'Codex can also answer questions or update the PR\. Try commenting "@codex address that feedback"\.'
    r"|"
    r"When you \[sign up for Codex through ChatGPT\]"
    r"\(https://openai\.com/codex\), Codex can also answer questions or update the PR, "
    r'like "@codex address that feedback"\.'
    r")"
    r"\s*</details>\n?\Z"
)
REVIEWED_COMMIT_RE = re.compile(r"(?m)^\*\*Reviewed commit:\*\* `(?P<commit>[0-9a-f]{10,40})`\s*$")


@dataclass(frozen=True)
class CodexOutcome:
    """One trusted connector outcome found while a candidate is pending."""

    occurred_at: datetime
    database_id: int
    kind: str


class CodexReviewRequiredError(GateError):
    """Raised when the live candidate still needs a trusted Codex outcome."""


def _bounded_body(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_OUTCOME_BODY_BYTES:
        return None
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _clean_reviewed_commit(value: object) -> str | None:
    body = _bounded_body(value)
    if body is None:
        return None
    match = CLEAN_OUTCOME_RE.search(body)
    markers = REVIEWED_COMMIT_RE.findall(body)
    if match is None or markers != [match.group("commit")]:
        return None
    flourish = match.group("flourish")
    if flourish is not None and (
        len(flourish.encode("utf-8")) > 80
        or flourish != flourish.strip()
        or any(
            unicodedata.category(character).startswith("C")
            or unicodedata.category(character) in {"Zl", "Zp"}
            for character in flourish
        )
    ):
        return None
    footer = match.group("footer")
    if footer not in {"", "\n"} and CLEAN_OUTCOME_FOOTER_RE.fullmatch(footer) is None:
        return None
    return match.group("commit")


def _is_connector_issue_comment(comment: dict[str, object]) -> bool:
    if not actor_is(
        comment,
        user_id=CODEX_CONNECTOR_USER_ID,
        user_login=CODEX_CONNECTOR_LOGIN,
    ):
        return False
    app = comment.get("performed_via_github_app")
    return (
        isinstance(app, dict)
        and app.get("id") == CODEX_CONNECTOR_APP_ID
        and app.get("slug") == CODEX_CONNECTOR_APP_SLUG
    )


def _formal_reviewed_commit(value: object) -> str | None:
    body = _bounded_body(value)
    if body is None or not body.lstrip("\n").startswith(FORMAL_REVIEW_PREFIX):
        return None
    markers = REVIEWED_COMMIT_RE.findall(body)
    return markers[0] if len(markers) == 1 else None


def _prefix_resolves_to_head(
    api: RestApi,
    *,
    repository: str,
    prefix: str,
    head_sha: str,
    cache: dict[str, bool],
) -> bool:
    if not head_sha.startswith(prefix):
        return False
    if len(prefix) == 40:
        return prefix == head_sha
    if prefix in cache:
        return cache[prefix]
    payload = record(
        api.get(f"/repos/{repository}/commits/{prefix}"),
        "Codex reviewed commit",
    )
    resolved = commit_sha(payload.get("sha"), "Codex reviewed commit") == head_sha
    cache[prefix] = resolved
    return resolved


def _exact_head_outcomes(
    api: RestApi,
    *,
    repository: str,
    pull_request: PullRequest,
    not_before: datetime,
    allow_clean_reaction: bool,
) -> list[CodexOutcome]:
    outcomes: list[CodexOutcome] = []
    resolved_prefixes: dict[str, bool] = {}
    comment_ids: set[int] = set()
    comments = api.paginate(f"/repos/{repository}/issues/{pull_request.number}/comments")
    for index, item in enumerate(comments):
        comment = record(item, f"Codex review outcome comment {index}")
        if not _is_connector_issue_comment(comment):
            continue
        comment_id = positive_int(comment.get("id"), "Codex outcome comment ID")
        if comment_id in comment_ids:
            raise GateError("Codex outcome comment ID is repeated")
        comment_ids.add(comment_id)
        created_at = timestamp(comment.get("created_at"), "Codex review outcome comment creation")
        updated_at = timestamp(comment.get("updated_at"), "Codex review outcome comment update")
        if updated_at < created_at:
            raise GateError("Codex review outcome comment update predates its creation")
        reviewed_commit = _clean_reviewed_commit(comment.get("body"))
        if (
            reviewed_commit is None
            or created_at <= not_before
            or not _prefix_resolves_to_head(
                api,
                repository=repository,
                prefix=reviewed_commit,
                head_sha=pull_request.head_sha,
                cache=resolved_prefixes,
            )
        ):
            continue
        outcomes.append(CodexOutcome(created_at, comment_id, "clean comment"))

    review_ids: set[int] = set()
    reviews = api.paginate(f"/repos/{repository}/pulls/{pull_request.number}/reviews")
    for index, item in enumerate(reviews):
        review = record(item, f"Codex review outcome {index}")
        if not actor_is(
            review,
            user_id=CODEX_CONNECTOR_USER_ID,
            user_login=CODEX_CONNECTOR_LOGIN,
        ):
            continue
        review_id = positive_int(review.get("id"), "Codex review outcome ID")
        if review_id in review_ids:
            raise GateError("Codex review outcome ID is repeated")
        review_ids.add(review_id)
        state = review.get("state")
        if state not in REVIEW_STATES:
            raise GateError("Codex review outcome state is invalid")
        submitted_at = timestamp(review.get("submitted_at"), "Codex review outcome submission")
        commit_id = commit_sha(review.get("commit_id"), "Codex review outcome commit")
        reviewed_commit = _formal_reviewed_commit(review.get("body"))
        if (
            state != "COMMENTED"
            or commit_id != pull_request.head_sha
            or reviewed_commit is None
            or submitted_at <= not_before
            or not _prefix_resolves_to_head(
                api,
                repository=repository,
                prefix=reviewed_commit,
                head_sha=pull_request.head_sha,
                cache=resolved_prefixes,
            )
        ):
            continue
        outcomes.append(CodexOutcome(submitted_at, review_id, "findings review"))

    if allow_clean_reaction:
        reaction_ids: set[int] = set()
        reactions = api.paginate(f"/repos/{repository}/issues/{pull_request.number}/reactions")
        for index, item in enumerate(reactions):
            reaction = record(item, f"Codex review reaction {index}")
            if not actor_is(
                reaction,
                user_id=CODEX_CONNECTOR_USER_ID,
                user_login=CODEX_CONNECTOR_LOGIN,
            ):
                continue
            reaction_id = positive_int(reaction.get("id"), "Codex review reaction ID")
            if reaction_id in reaction_ids:
                raise GateError("Codex review reaction ID is repeated")
            reaction_ids.add(reaction_id)
            content = reaction.get("content")
            if content not in REACTION_CONTENTS:
                raise GateError("Codex review reaction content is invalid")
            if content != "+1":
                continue
            created_at = timestamp(reaction.get("created_at"), "Codex review reaction creation")
            # `allow_clean_reaction` is restricted to the initial ready `opened`
            # boundary. The PR cannot have a connector reaction before it exists,
            # so equality at GitHub's one-second timestamp resolution is safe.
            if created_at >= not_before:
                outcomes.append(CodexOutcome(created_at, reaction_id, "clean automatic reaction"))
    return outcomes


def check_codex_outcome_policy(
    api: RestApi,
    *,
    repository: str,
    pull_request: PullRequest,
    not_before: datetime,
    allow_clean_reaction: bool,
) -> str:
    """Require a trusted connector outcome after one recorded candidate boundary."""
    outcomes = _exact_head_outcomes(
        api,
        repository=repository,
        pull_request=pull_request,
        not_before=not_before,
        allow_clean_reaction=allow_clean_reaction,
    )
    if not outcomes:
        raise CodexReviewRequiredError("current head and base need a trusted Codex review outcome")
    latest = max(outcomes, key=lambda item: (item.occurred_at, item.database_id, item.kind))
    return f"trusted exact-head Codex {latest.kind} is current"


def connector_actor(value: object, label: str) -> None:
    """Require the exact Codex connector identity for trusted workflow evidence."""
    actor = record(value, label)
    if actor.get("id") != CODEX_CONNECTOR_USER_ID or actor.get("login") != CODEX_CONNECTOR_LOGIN:
        raise GateError(f"{label} is not the Codex connector")
