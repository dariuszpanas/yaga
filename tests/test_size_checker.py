from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import InputError
from yaga.sizes import checker
from yaga.sizes.checker import check_sizes
from yaga.sizes.models import (
    MAX_SIZE_TOTAL_BYTES,
    SIZE_BLOB_CODE,
    SIZE_TOTAL_CODE,
    BlobEntry,
    SizePathLimit,
    SizePolicy,
    SizeSelection,
)


def _selection(tmp_path: Path, *blobs: BlobEntry, gitlinks: tuple[str, ...] = ()) -> SizeSelection:
    return SizeSelection(
        repository=tmp_path.resolve(),
        revision="HEAD",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        blobs=tuple(sorted(blobs, key=lambda item: item.path)),
        gitlinks=gitlinks,
    )


def test_check_sizes_applies_first_override_and_orders_blob_then_total(
    tmp_path: Path,
) -> None:
    policy = SizePolicy(
        1,
        10,
        20,
        (
            SizePathLimit("assets/**", 12),
            SizePathLimit("assets/*.map", 100),
        ),
    )
    selection = _selection(
        tmp_path,
        BlobEntry("assets/app.map", "c" * 40, "100644", 13),
        BlobEntry("z.bin", "d" * 40, "100644", 11),
    )

    report = check_sizes(policy, selection)

    assert [(item.code, item.path, item.pattern, item.limit) for item in report.diagnostics] == [
        (SIZE_BLOB_CODE, "assets/app.map", "assets/**", 12),
        (SIZE_BLOB_CODE, "z.bin", None, 10),
        (SIZE_TOTAL_CODE, None, None, 20),
    ]
    assert report.total_bytes == 24


def test_check_sizes_counts_duplicate_oids_and_symlink_text_but_excludes_gitlinks(
    tmp_path: Path,
) -> None:
    policy = SizePolicy(1, 100, 14)
    selection = _selection(
        tmp_path,
        BlobEntry("a", "c" * 40, "100644", 5),
        BlobEntry("b", "c" * 40, "100755", 5),
        BlobEntry("link", "d" * 40, "120000", 5),
        gitlinks=("vendor/submodule",),
    )

    report = check_sizes(policy, selection)

    assert report.total_bytes == 15
    assert [item.code for item in report.diagnostics] == [SIZE_TOTAL_CODE]


def test_check_sizes_treats_limit_equality_and_lfs_pointer_as_ordinary_blob(
    tmp_path: Path,
) -> None:
    selection = _selection(
        tmp_path,
        BlobEntry("asset.bin", "c" * 40, "100644", 128),
        BlobEntry("asset.lfs", "d" * 40, "100644", 129),
    )

    report = check_sizes(SizePolicy(1, 128), selection)

    assert [(item.code, item.path) for item in report.diagnostics] == [
        (SIZE_BLOB_CODE, "asset.lfs")
    ]


def test_check_sizes_maps_match_work_exhaustion_to_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = SizePolicy(1, 10, None, (SizePathLimit("**", 10),))
    selection = _selection(tmp_path, BlobEntry("file", "c" * 40, "100644", 1))
    monkeypatch.setattr(checker, "MAX_SIZE_MATCH_WORK", 0)

    with pytest.raises(InputError, match="0-unit match-work limit"):
        check_sizes(policy, selection)


def test_check_sizes_uses_one_policy_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = SizePolicy(1, 10, None, (SizePathLimit("**", 10),))
    selection = _selection(tmp_path, BlobEntry("file", "c" * 40, "100644", 1))
    original = checker._evaluate_size_policy
    calls = 0

    def counted_evaluation(
        selected_policy: SizePolicy,
        selected_tree: SizeSelection,
        *,
        match_work_limit: int | None = None,
    ) -> object:
        nonlocal calls
        calls += 1
        return original(
            selected_policy,
            selected_tree,
            match_work_limit=match_work_limit,
        )

    monkeypatch.setattr(checker, "_evaluate_size_policy", counted_evaluation)

    assert check_sizes(policy, selection).valid
    assert calls == 1


def test_check_sizes_enforces_portable_aggregate_integer_boundary(tmp_path: Path) -> None:
    exact = _selection(
        tmp_path,
        BlobEntry("exact", "c" * 40, "100644", MAX_SIZE_TOTAL_BYTES),
    )
    overflow = _selection(
        tmp_path,
        BlobEntry("a", "c" * 40, "100644", MAX_SIZE_TOTAL_BYTES),
        BlobEntry("b", "d" * 40, "100644", 1),
    )

    assert check_sizes(SizePolicy(1, MAX_SIZE_TOTAL_BYTES), exact).total_bytes == (
        MAX_SIZE_TOTAL_BYTES
    )
    with pytest.raises(
        InputError,
        match=rf"hard {MAX_SIZE_TOTAL_BYTES}-byte portable-integer limit",
    ):
        check_sizes(SizePolicy(1, MAX_SIZE_TOTAL_BYTES), overflow)


def test_check_sizes_rejects_wrong_model_types(tmp_path: Path) -> None:
    policy = SizePolicy(1, 10)
    selection = _selection(tmp_path)

    with pytest.raises(TypeError, match="SizePolicy"):
        check_sizes(cast(Any, object()), selection)
    with pytest.raises(TypeError, match="SizeSelection"):
        check_sizes(policy, cast(Any, object()))
