"""Shared resolution of configured per-type presence requirements."""

from yaga.commits.models import PresencePolicy


def resolve_presence_policy(
    default: PresencePolicy,
    overrides: tuple[tuple[str, PresencePolicy], ...],
    commit_type: str,
) -> tuple[PresencePolicy, str | None]:
    """Return the effective requirement and matching configured key, if any."""
    folded = commit_type.casefold()
    for key, value in overrides:
        if key.casefold() == folded:
            return value, key
    return default, None
