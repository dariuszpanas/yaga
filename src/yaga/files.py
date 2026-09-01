"""Small dependency-free primitives for bounded local file reads."""

from __future__ import annotations

from pathlib import Path


def read_file_prefix(path: Path, *, maximum: int) -> bytes:
    """Read at most ``maximum + 1`` bytes so callers can detect overflow."""
    if maximum < 0:
        raise ValueError("maximum file size must be nonnegative")
    with path.open("rb") as stream:
        return stream.read(maximum + 1)
