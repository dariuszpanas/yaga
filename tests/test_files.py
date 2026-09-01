"""Tests for dependency-free bounded file primitives."""

from __future__ import annotations

import io
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from yaga.files import read_file_prefix


class GuardedReader(io.BytesIO):
    """Fail if the helper ever attempts an unbounded read."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_type, exc_val, exc_tb
        self.close()

    def read(self, size: int | None = -1, /) -> bytes:
        assert size == 33
        return super().read(size)


def test_file_prefix_reads_only_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reader = GuardedReader(b"x" * 100)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: reader)

    assert read_file_prefix(tmp_path / "large", maximum=32) == b"x" * 33


def test_file_prefix_rejects_a_negative_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        read_file_prefix(tmp_path / "unused", maximum=-1)
