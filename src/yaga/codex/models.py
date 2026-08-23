"""Strict payload models specific to the Codex review gate."""

from __future__ import annotations

import json
from dataclasses import dataclass

from yaga.errors import GateError
from yaga.models import commit_sha, positive_int, record, ref_name, repository_name

MAX_CANDIDATE_BYTES = 4_096


@dataclass(frozen=True)
class Candidate:
    """Exact pull-request identity passed between bounded workflow phases."""

    pull_request_number: int
    head_sha: str
    base_sha: str
    base_ref: str
    head_repository: str
    head_ref: str

    def __post_init__(self) -> None:
        """Keep directly constructed candidates as strict as decoded ones."""
        object.__setattr__(
            self,
            "pull_request_number",
            positive_int(self.pull_request_number, "Codex review candidate pull request"),
        )
        object.__setattr__(
            self,
            "head_sha",
            commit_sha(self.head_sha, "Codex review candidate head"),
        )
        object.__setattr__(
            self,
            "base_sha",
            commit_sha(self.base_sha, "Codex review candidate base"),
        )
        object.__setattr__(
            self,
            "base_ref",
            ref_name(self.base_ref, "Codex review candidate base ref"),
        )
        object.__setattr__(self, "head_repository", repository_name(self.head_repository))
        object.__setattr__(
            self,
            "head_ref",
            ref_name(self.head_ref, "Codex review candidate head ref"),
        )

    def to_payload(self) -> dict[str, object]:
        """Return the closed JSON shape accepted by :meth:`from_json`."""
        return {
            "base": self.base_sha,
            "base_ref": self.base_ref,
            "head": self.head_sha,
            "head_ref": self.head_ref,
            "head_repository": self.head_repository,
            "pr": self.pull_request_number,
        }

    def to_json(self) -> str:
        """Serialize compactly for a GitHub Actions matrix or output."""
        return json.dumps(self.to_payload(), separators=(",", ":"))

    @classmethod
    def from_json(cls, value: object) -> Candidate:
        """Parse one strict candidate capability."""
        if not isinstance(value, str) or not value or len(value) > MAX_CANDIDATE_BYTES:
            raise GateError("Codex review candidate is invalid")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise GateError("Codex review candidate is invalid") from error
        if len(encoded) > MAX_CANDIDATE_BYTES:
            raise GateError("Codex review candidate is invalid")
        try:
            payload = record(json.loads(value), "Codex review candidate")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GateError("Codex review candidate is invalid") from error
        expected_fields = {
            "base",
            "base_ref",
            "head",
            "head_ref",
            "head_repository",
            "pr",
        }
        if set(payload) != expected_fields:
            raise GateError("Codex review candidate fields are invalid")
        return cls(
            pull_request_number=positive_int(
                payload.get("pr"), "Codex review candidate pull request"
            ),
            head_sha=commit_sha(payload.get("head"), "Codex review candidate head"),
            base_sha=commit_sha(payload.get("base"), "Codex review candidate base"),
            base_ref=ref_name(payload.get("base_ref"), "Codex review candidate base ref"),
            head_repository=repository_name(payload.get("head_repository")),
            head_ref=ref_name(payload.get("head_ref"), "Codex review candidate head ref"),
        )
