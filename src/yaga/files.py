"""Small dependency-free primitives for bounded local file reads."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_file_prefix(path: Path, *, maximum: int) -> bytes:
    """Read at most ``maximum + 1`` bytes so callers can detect overflow."""
    if maximum < 0:
        raise ValueError("maximum file size must be nonnegative")
    with path.open("rb") as stream:
        return stream.read(maximum + 1)


def read_regular_file_prefix(path: Path, *, maximum: int) -> bytes:
    """Read a bounded regular file, rejecting special files and open-time replacement.

    Callers may resolve a symlink before calling. The selected path itself must be
    regular, including after opening; nonblocking opens guard POSIX FIFO races.
    Return up to ``maximum + 1`` bytes so callers retain overflow handling.
    """
    if maximum < 0:
        raise ValueError("maximum file size must be nonnegative")
    before_open = path.lstat()
    if not stat.S_ISREG(before_open.st_mode):
        raise OSError("input must be a regular file")
    flags = os.O_RDONLY
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("input must be a regular file")
        if (before_open.st_dev, before_open.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("input changed while being opened")
        output = bytearray()
        while len(output) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
        return bytes(output)
    finally:
        os.close(descriptor)
