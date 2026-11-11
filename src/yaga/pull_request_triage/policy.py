"""Load one bounded, explicit trust policy; never discover policy in a PR."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from yaga.errors import ConfigurationError
from yaga.files import read_file_prefix
from yaga.path_patterns import validate_pattern
from yaga.policies.schemas import TRIAGE_POLICY_FAMILY, validate_family_wire

MAX_POLICY_BYTES = 64 * 1024
DEFAULT_SENSITIVE_PATTERNS = (
    ".github/**",
    ".yaga/**",
    ".yaga.toml",
    "**/AGENTS.md",
    "**/CODEOWNERS",
    "**/SECURITY.md",
    "**/CONTRIBUTING.md",
)


@dataclass(frozen=True, slots=True)
class TrustedBot:
    user_id: int
    login: str
    allowed_patterns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TriagePolicy:
    host: str
    repository_id: int
    maintainer_user_ids: tuple[int, ...]
    trusted_bots: tuple[TrustedBot, ...]
    sensitive_patterns: tuple[str, ...]
    duplicate_candidates: int
    digest: str
    comment_author_id: int | None = None


def _positive(value: object, label: str) -> int:
    if type(value) is not int or not 1 <= value <= (1 << 53) - 1:
        raise ConfigurationError(f"{label} must be a positive portable integer")
    return value


def _patterns(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ConfigurationError(f"{label} must contain 1..64 patterns")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigurationError(f"{label} must contain strings")
        try:
            validate_pattern(item)
        except ValueError:
            raise ConfigurationError(f"{label} contains an invalid path pattern") from None
        result.append(item)
    if len(set(result)) != len(result):
        raise ConfigurationError(f"{label} contains duplicate patterns")
    return tuple(result)


def load_triage_policy(path: Path) -> TriagePolicy:
    try:
        raw = read_file_prefix(path.expanduser().resolve(), maximum=MAX_POLICY_BYTES)
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError("cannot read explicit pull-request triage policy") from None
    if len(raw) > MAX_POLICY_BYTES:
        raise ConfigurationError("pull-request triage policy exceeds 64 KiB")
    try:
        document = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ConfigurationError("invalid pull-request triage policy TOML") from None
    validate_family_wire(
        TRIAGE_POLICY_FAMILY,
        document,
        version_key="triage-policy-version",
        version_error_message="triage-policy-version must be integer 1",
    )
    host = document["host"]
    if (
        not isinstance(host, str)
        or not host.isascii()
        or host != host.lower()
        or len(host) > 253
        or not all(
            part
            and part[0].isalnum()
            and part[-1].isalnum()
            and all(char.isalnum() or char == "-" for char in part)
            for part in host.split(".")
        )
    ):
        raise ConfigurationError("triage host must be one lowercase DNS hostname")
    repository_id = _positive(document["repository-id"], "repository-id")
    maintainers = document["maintainer-user-ids"]
    if not isinstance(maintainers, list) or len(maintainers) > 32:
        raise ConfigurationError("maintainer-user-ids must contain at most 32 IDs")
    ids = tuple(_positive(item, "maintainer-user-ids") for item in maintainers)
    if len(set(ids)) != len(ids):
        raise ConfigurationError("maintainer-user-ids contains duplicate IDs")
    bots = []
    raw_bots = document.get("trusted-bots", [])
    if not isinstance(raw_bots, list) or len(raw_bots) > 32:
        raise ConfigurationError("trusted-bots must contain at most 32 records")
    for bot in raw_bots:
        user_id = _positive(bot["user-id"], "trusted-bots.user-id")
        login = bot["login"]
        if (
            not isinstance(login, str)
            or not login.isascii()
            or not 1 <= len(login) <= 100
            or any(not (c.isalnum() or c in "-[]") for c in login)
        ):
            raise ConfigurationError("trusted bot login is invalid")
        bots.append(
            TrustedBot(user_id, login, _patterns(bot["allowed-patterns"], "bot allowed-patterns"))
        )
    if len({bot.user_id for bot in bots}) != len(bots) or set(ids) & {bot.user_id for bot in bots}:
        raise ConfigurationError("triage identities overlap or repeat")
    candidate_count = document.get("duplicate-candidates", 5)
    if type(candidate_count) is not int or not 0 <= candidate_count <= 20:
        raise ConfigurationError("duplicate-candidates must be an integer in 0..20")
    return TriagePolicy(
        host,
        repository_id,
        ids,
        tuple(bots),
        _patterns(
            document.get("sensitive-patterns", list(DEFAULT_SENSITIVE_PATTERNS)),
            "sensitive-patterns",
        ),
        candidate_count,
        hashlib.sha256(raw).hexdigest(),
        _positive(document["comment-author-id"], "comment-author-id")
        if "comment-author-id" in document
        else None,
    )
