"""Bounded explicit commit-message input sources."""

from __future__ import annotations

import sys
from pathlib import Path

from yaga.commits.models import CommitTarget
from yaga.commits.parser import MAX_MESSAGE_BYTES
from yaga.errors import InputError
from yaga.files import read_file_prefix


def from_message(message: str) -> CommitTarget:
    """Build a target from an explicit command-line message."""
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError("explicit message is not valid UTF-8") from error
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise InputError(f"explicit message exceeds the hard {MAX_MESSAGE_BYTES}-byte limit")
    return CommitTarget(label="message", message=message)


def from_file(path: Path) -> CommitTarget:
    """Read one UTF-8 commit message from a file."""
    resolved = path.expanduser().resolve()
    try:
        raw = read_file_prefix(resolved, maximum=MAX_MESSAGE_BYTES)
    except OSError as error:
        raise InputError(f"cannot read message file {resolved}: {error}") from error
    if len(raw) > MAX_MESSAGE_BYTES:
        raise InputError(f"message file exceeds the hard {MAX_MESSAGE_BYTES}-byte limit")
    try:
        message = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputError(f"message file is not valid UTF-8: {resolved}") from error
    return CommitTarget(label=str(resolved), message=message)


def from_stdin() -> CommitTarget:
    """Read one bounded message from standard input."""
    binary_stream = getattr(sys.stdin, "buffer", None)
    if binary_stream is not None:
        try:
            raw = binary_stream.read(MAX_MESSAGE_BYTES + 1)
        except (OSError, UnicodeError, ValueError) as error:
            raise InputError("standard input could not be read as UTF-8") from error
        if not isinstance(raw, bytes):
            raise InputError("standard input did not provide a byte stream")
        if len(raw) > MAX_MESSAGE_BYTES:
            raise InputError(f"standard input exceeds the hard {MAX_MESSAGE_BYTES}-byte limit")
        try:
            message = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InputError("standard input is not valid UTF-8") from error
        return CommitTarget(label="stdin", message=message)

    try:
        message = sys.stdin.read(MAX_MESSAGE_BYTES + 1)
        encoded = message.encode("utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        raise InputError("standard input could not be read as UTF-8") from error
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise InputError(f"standard input exceeds the hard {MAX_MESSAGE_BYTES}-byte limit")
    return CommitTarget(label="stdin", message=message)
