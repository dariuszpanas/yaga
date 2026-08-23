"""Generic, strict GitHub commit-status models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from yaga.errors import GateError
from yaga.models import bounded_text, login, positive_int, record, timestamp

MAX_STATUS_CONTEXT_BYTES = 100
MAX_STATUS_DESCRIPTION_BYTES = 140
MAX_STATUS_TARGET_URL_BYTES = 2_048
STATUS_STATES = frozenset({"error", "failure", "pending", "success"})


@dataclass(frozen=True)
class CommitStatus:
    """Security-relevant fields from one GitHub commit status."""

    status_id: int
    state: str
    context: str
    description: str | None
    target_url: str | None
    created_at: datetime
    creator_id: int
    creator_login: str


def _nullable_text(value: object, label: str, *, max_bytes: int) -> str | None:
    if value is None:
        return None
    return bounded_text(value, label, max_bytes=max_bytes, allow_empty=True)


def parse_commit_status(value: object, *, label: str = "commit status") -> CommitStatus:
    """Parse one complete status payload without accepting unknown state values."""
    payload = record(value, label)
    state = payload.get("state")
    if state not in STATUS_STATES:
        raise GateError(f"{label} state is invalid")
    creator = record(payload.get("creator"), f"{label} creator")
    return CommitStatus(
        status_id=positive_int(payload.get("id"), f"{label} ID"),
        state=state,
        context=bounded_text(
            payload.get("context"), f"{label} context", max_bytes=MAX_STATUS_CONTEXT_BYTES
        ),
        description=_nullable_text(
            payload.get("description"),
            f"{label} description",
            max_bytes=MAX_STATUS_DESCRIPTION_BYTES,
        ),
        target_url=_nullable_text(
            payload.get("target_url"),
            f"{label} target URL",
            max_bytes=MAX_STATUS_TARGET_URL_BYTES,
        ),
        created_at=timestamp(payload.get("created_at"), f"{label} creation"),
        creator_id=positive_int(creator.get("id"), f"{label} creator ID"),
        creator_login=login(creator.get("login"), f"{label} creator login"),
    )
