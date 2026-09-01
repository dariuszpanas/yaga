from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yaga.sizes.checker import check_sizes
from yaga.sizes.models import (
    MAX_SIZE_BYTES,
    SIZE_BLOB_CODE,
    SIZE_TOTAL_CODE,
    BlobEntry,
    SizeDiagnostic,
    SizePathLimit,
    SizePolicy,
    SizeReport,
    SizeSelection,
)

OID = "a" * 40
TREE_OID = "b" * 40


def _selection(tmp_path: Path, *blobs: BlobEntry) -> SizeSelection:
    return SizeSelection(
        repository=tmp_path.resolve(),
        revision="HEAD",
        commit_sha=OID,
        tree_sha=TREE_OID,
        blobs=tuple(sorted(blobs, key=lambda item: item.path)),
        gitlinks=(),
    )


def test_size_policy_accepts_minimal_default_only_policy() -> None:
    policy = SizePolicy(size_policy_version=1, default_max_blob_bytes=128)

    assert policy.max_total_blob_bytes is None
    assert policy.path_limits == ()


@pytest.mark.parametrize("value", [True, -1, MAX_SIZE_BYTES + 1, 1.5, "1"])
def test_size_policy_rejects_non_exact_or_out_of_range_byte_limits(value: Any) -> None:
    with pytest.raises(ValueError, match="integer from 0 through"):
        SizePolicy(size_policy_version=1, default_max_blob_bytes=value)


def test_size_policy_requires_ordered_unique_valid_path_limits() -> None:
    limits = (
        SizePathLimit("uv.lock", 1_000),
        SizePathLimit("assets/**", 2_000),
    )
    policy = SizePolicy(1, 100, 10_000, limits)

    assert policy.path_limits == limits
    with pytest.raises(ValueError, match="unique"):
        SizePolicy(1, 100, None, (limits[0], limits[0]))
    with pytest.raises(ValueError, match="size pattern"):
        SizePathLimit("bad/**suffix", 1)


def test_blob_entry_and_selection_preserve_logical_duplicate_oid_paths(
    tmp_path: Path,
) -> None:
    first = BlobEntry("a.bin", OID, "100644", 25)
    second = BlobEntry("b.bin", OID, "100755", 25)
    link = BlobEntry("link", "c" * 40, "120000", 8)
    selection = SizeSelection(
        repository=tmp_path.resolve(),
        revision="release-candidate",
        commit_sha=OID,
        tree_sha=TREE_OID,
        blobs=(first, second, link),
        gitlinks=("vendor/submodule",),
    )

    assert sum(item.size for item in selection.blobs) == 58
    assert selection.gitlinks == ("vendor/submodule",)


@pytest.mark.parametrize("mode", ["100600", "040000", "160000", "blob"])
def test_blob_entry_rejects_non_blob_modes(mode: str) -> None:
    with pytest.raises(ValueError, match="mode"):
        BlobEntry("file", OID, mode, 1)


def test_size_selection_enforces_identity_and_lexical_disjoint_paths(tmp_path: Path) -> None:
    first = BlobEntry("a", OID, "100644", 1)
    second = BlobEntry("b", "c" * 40, "100644", 2)

    with pytest.raises(ValueError, match="repository"):
        SizeSelection(Path("."), "HEAD", OID, TREE_OID, (), ())
    with pytest.raises(ValueError, match="unique and sorted"):
        SizeSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, (second, first), ())
    with pytest.raises(ValueError, match="disjoint"):
        SizeSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, (first,), ("a",))
    with pytest.raises(ValueError, match="object format"):
        SizeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (BlobEntry("a", "d" * 64, "100644", 1),),
            (),
        )
    with pytest.raises(ValueError, match="inconsistent sizes"):
        SizeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (
                BlobEntry("a", "d" * 40, "100644", 1),
                BlobEntry("b", "d" * 40, "100755", 2),
            ),
            (),
        )
    with pytest.raises(ValueError, match="contain another leaf"):
        SizeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (BlobEntry("vendor/file", "d" * 40, "100644", 1),),
            ("vendor",),
        )
    with pytest.raises(ValueError, match="contain another leaf"):
        SizeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (
                BlobEntry("a", "d" * 40, "100644", 1),
                BlobEntry("a-", "e" * 40, "100644", 1),
                BlobEntry("a/b", "f" * 40, "100644", 1),
            ),
            (),
        )
    with pytest.raises(ValueError, match="contain another leaf"):
        SizeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (
                BlobEntry("a-", "d" * 40, "100644", 1),
                BlobEntry("a/b", "e" * 40, "100644", 1),
            ),
            ("a",),
        )
    with pytest.raises(ValueError, match="contain another leaf"):
        SizeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (
                BlobEntry("a", "d" * 40, "100644", 1),
                BlobEntry("a-", "e" * 40, "100644", 1),
            ),
            ("a/b",),
        )


def test_size_report_requires_lexical_blob_findings_then_exact_aggregate(
    tmp_path: Path,
) -> None:
    policy = SizePolicy(1, 5, 10)
    selection = _selection(
        tmp_path,
        BlobEntry("a", OID, "100644", 6),
        BlobEntry("b", "c" * 40, "100644", 7),
    )
    report = check_sizes(policy, selection)
    diagnostics = report.diagnostics

    assert report.status == "failed"
    assert not report.valid
    assert report.total_bytes == 13
    assert report.oversized_blobs == diagnostics[:2]
    assert report.total_exceeded

    with pytest.raises(ValueError, match="precede"):
        SizeReport(policy, selection, (diagnostics[-1], diagnostics[0]))
    with pytest.raises(ValueError, match="aggregate"):
        SizeReport(policy, selection, diagnostics[:2])


def test_size_report_rejects_missing_findings_and_non_first_override(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path, BlobEntry("assets/app.bin", OID, "100644", 2))
    default_policy = SizePolicy(1, 1)

    with pytest.raises(ValueError, match="complete exact ordered"):
        SizeReport(default_policy, selection, ())

    first_match_policy = SizePolicy(
        1,
        100,
        None,
        (SizePathLimit("**", 100), SizePathLimit("assets/**", 1)),
    )
    later_override = SizeDiagnostic(
        SIZE_BLOB_CODE,
        "committed blob exceeds its configured byte limit",
        2,
        1,
        "assets/app.bin",
        "assets/**",
    )
    with pytest.raises(ValueError, match="complete exact ordered"):
        SizeReport(first_match_policy, selection, (later_override,))


def test_size_diagnostic_rejects_values_above_portable_integer_range() -> None:
    with pytest.raises(ValueError, match="integer from 0 through"):
        SizeDiagnostic(
            SIZE_TOTAL_CODE,
            "total",
            MAX_SIZE_BYTES + 1,
            MAX_SIZE_BYTES,
        )
