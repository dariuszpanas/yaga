"""Regular-file reads retain bounds and reject special files before blocking."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from yaga.files import read_regular_file_prefix


def test_regular_file_prefix_reads_only_limit_plus_one(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"x" * 100)
    assert read_regular_file_prefix(path, maximum=32) == b"x" * 33
    assert read_regular_file_prefix(path, maximum=0) == b"x"
    assert read_regular_file_prefix(path, maximum=100) == b"x" * 100
    with pytest.raises(ValueError, match="nonnegative"):
        read_regular_file_prefix(path, maximum=-1)


def test_special_file_is_rejected_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_open(*_args: object, **_kwargs: object) -> int:
        pytest.fail("must reject a directory without opening it")

    monkeypatch.setattr(os, "open", forbidden_open)
    with pytest.raises(OSError, match="regular file"):
        read_regular_file_prefix(tmp_path, maximum=32)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special-file semantics")
def test_fifo_rejected_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "fifo"
    mkfifo = getattr(os, "mkfifo", None)
    assert mkfifo is not None
    mkfifo(path)
    with pytest.raises(OSError, match="regular file"):
        read_regular_file_prefix(path, maximum=32)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX special-file semantics")
def test_fifo_replacement_uses_nonblocking_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"content")
    original_open = os.open
    mkfifo = getattr(os, "mkfifo", None)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    assert mkfifo is not None and nonblocking

    def replace_before_open(selected: Path, flags: int) -> int:
        assert flags & nonblocking
        selected.unlink()
        mkfifo(selected)
        return original_open(selected, flags)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises(OSError, match="regular file"):
        read_regular_file_prefix(path, maximum=32)


@pytest.mark.parametrize("change", ["type", "identity"])
def test_open_time_revalidation_rejects_replacement_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"content")
    original_fstat = os.fstat
    original_close = os.close
    closed: list[int] = []

    def changed_fstat(descriptor: int) -> os.stat_result:
        metadata = list(original_fstat(descriptor))
        if change == "type":
            metadata[0] = stat.S_IFIFO
        else:
            metadata[1] += 1
        return os.stat_result(metadata)

    def track_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "fstat", changed_fstat)
    monkeypatch.setattr(os, "close", track_close)
    with pytest.raises(OSError, match="regular file|changed while"):
        read_regular_file_prefix(path, maximum=32)
    assert len(closed) == 1


def test_read_failure_still_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file"
    path.write_bytes(b"content")
    original_close = os.close
    closed: list[int] = []

    def fail_read(_descriptor: int, _size: int) -> bytes:
        raise OSError("read failed")

    def track_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(os, "read", fail_read)
    monkeypatch.setattr(os, "close", track_close)
    with pytest.raises(OSError, match="read failed"):
        read_regular_file_prefix(path, maximum=32)
    assert len(closed) == 1
