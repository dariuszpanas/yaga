from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yaga.modes.checker import check_modes
from yaga.modes.models import (
    MODE_DISALLOWED_CODE,
    MODE_KINDS,
    ModeDiagnostic,
    ModeEntry,
    ModeKind,
    ModePathOverride,
    ModePolicy,
    ModeReport,
    ModeSelection,
)

OID = "a" * 40
TREE_OID = "b" * 40


def _entry(
    path: str,
    *,
    mode: str = "100644",
    object_type: str = "blob",
    oid: str = OID,
) -> ModeEntry:
    return ModeEntry(path, oid, mode, object_type)


def _selection(tmp_path: Path, *entries: ModeEntry) -> ModeSelection:
    return ModeSelection(
        repository=tmp_path.resolve(),
        revision="HEAD",
        commit_sha=OID,
        tree_sha=TREE_OID,
        entries=tuple(sorted(entries, key=lambda item: item.path)),
    )


def test_mode_kind_and_entry_map_exact_git_pairs_in_canonical_order() -> None:
    entries = (
        _entry("regular", mode="100644"),
        _entry("executable", mode="100755"),
        _entry("symlink", mode="120000"),
        _entry("gitlink", mode="160000", object_type="commit"),
    )

    assert MODE_KINDS == (
        ModeKind.REGULAR,
        ModeKind.EXECUTABLE,
        ModeKind.SYMLINK,
        ModeKind.GITLINK,
    )
    assert tuple(entry.kind for entry in entries) == MODE_KINDS


@pytest.mark.parametrize(
    ("mode", "object_type"),
    [
        ("100644", "commit"),
        ("100755", "tree"),
        ("120000", "commit"),
        ("160000", "blob"),
        ("040000", "tree"),
        ("100600", "blob"),
    ],
)
def test_mode_entry_rejects_unsupported_or_mismatched_git_pairs(
    mode: str,
    object_type: str,
) -> None:
    with pytest.raises(ValueError, match="mode/object-type pair"):
        _entry("file", mode=mode, object_type=object_type)


def test_mode_policy_canonicalizes_unique_allowed_mode_sets() -> None:
    override = ModePathOverride(
        "scripts/**",
        (ModeKind.SYMLINK, ModeKind.EXECUTABLE),
    )
    policy = ModePolicy(
        1,
        (ModeKind.GITLINK, ModeKind.REGULAR),
        (override,),
    )

    assert policy.default_allowed_modes == (ModeKind.REGULAR, ModeKind.GITLINK)
    assert override.allowed_modes == (ModeKind.EXECUTABLE, ModeKind.SYMLINK)


@pytest.mark.parametrize(
    "value",
    [
        (),
        (ModeKind.REGULAR, ModeKind.REGULAR),
        ("regular",),
        [ModeKind.REGULAR],
    ],
)
def test_mode_policy_rejects_empty_duplicate_or_non_enum_modes(value: Any) -> None:
    with pytest.raises(ValueError, match="allowed modes"):
        ModePolicy(1, value)


def test_mode_policy_preserves_ordered_unique_path_overrides() -> None:
    first = ModePathOverride("scripts/**", (ModeKind.EXECUTABLE,))
    second = ModePathOverride("**/*.sh", (ModeKind.REGULAR,))

    assert ModePolicy(1, (ModeKind.REGULAR,), (first, second)).path_overrides == (
        first,
        second,
    )
    with pytest.raises(ValueError, match="unique and case-sensitive"):
        ModePolicy(1, (ModeKind.REGULAR,), (first, first))
    with pytest.raises(ValueError, match="mode pattern"):
        ModePathOverride("bad/**suffix", (ModeKind.REGULAR,))


def test_mode_selection_preserves_policy_relevant_windows_invalid_names(
    tmp_path: Path,
) -> None:
    selection = _selection(
        tmp_path,
        _entry("aux:?.txt"),
        _entry("literal\\backslash"),
        _entry("trailing. ", mode="120000"),
    )

    assert tuple(entry.path for entry in selection.entries) == (
        "aux:?.txt",
        "literal\\backslash",
        "trailing. ",
    )


def test_mode_selection_enforces_identity_lexical_order_and_leaf_topology(
    tmp_path: Path,
) -> None:
    first = _entry("a")
    second = _entry("b", oid="c" * 40)

    with pytest.raises(ValueError, match="repository"):
        ModeSelection(
            Path("."),
            "HEAD",
            OID,
            TREE_OID,
            (),
        )
    with pytest.raises(ValueError, match="unique and sorted"):
        ModeSelection(tmp_path.resolve(), "HEAD", OID, TREE_OID, (second, first))
    with pytest.raises(ValueError, match="object format"):
        ModeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (_entry("a", oid="d" * 64),),
        )
    with pytest.raises(ValueError, match="contain another leaf"):
        ModeSelection(
            tmp_path.resolve(),
            "HEAD",
            OID,
            TREE_OID,
            (_entry("a"), _entry("a-", oid="c" * 40), _entry("a/b", oid="d" * 40)),
        )


@pytest.mark.parametrize(
    "path",
    ["", "/absolute", "./file", "dir/../file", "dir//file", "nul\x00name", "x/"],
)
def test_mode_entry_rejects_structurally_invalid_paths(path: str) -> None:
    with pytest.raises(ValueError, match="mode path"):
        _entry(path)


@pytest.mark.parametrize(
    "revision",
    ["", "--all", ":vendor", "HEAD:vendor", "^HEAD", "A..B", "head ref", "line\nbreak"],
)
def test_mode_selection_rejects_unsafe_revision(revision: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mode revision"):
        ModeSelection(tmp_path.resolve(), revision, OID, TREE_OID, ())


def test_mode_diagnostic_validates_actual_kind_policy_and_pattern() -> None:
    diagnostic = ModeDiagnostic(
        MODE_DISALLOWED_CODE,
        "disallowed",
        "script.sh",
        "100755",
        ModeKind.EXECUTABLE,
        (ModeKind.REGULAR,),
        "**/*.sh",
    )

    assert diagnostic.actual_kind is ModeKind.EXECUTABLE
    with pytest.raises(ValueError, match="does not match"):
        ModeDiagnostic(
            MODE_DISALLOWED_CODE,
            "disallowed",
            "script.sh",
            "100644",
            ModeKind.EXECUTABLE,
            (ModeKind.REGULAR,),
        )
    with pytest.raises(ValueError, match="must be disallowed"):
        ModeDiagnostic(
            MODE_DISALLOWED_CODE,
            "disallowed",
            "script.sh",
            "100755",
            ModeKind.EXECUTABLE,
            (ModeKind.EXECUTABLE,),
        )


def test_mode_report_is_sealed_against_missing_and_non_first_match_findings(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path, _entry("scripts/tool.sh", mode="100755"))
    default_policy = ModePolicy(1, (ModeKind.REGULAR,))

    with pytest.raises(ValueError, match="complete exact ordered"):
        ModeReport(default_policy, selection, ())

    first_match_policy = ModePolicy(
        1,
        (ModeKind.REGULAR,),
        (
            ModePathOverride("scripts/**", (ModeKind.EXECUTABLE,)),
            ModePathOverride("**/*.sh", (ModeKind.REGULAR,)),
        ),
    )
    forged = ModeDiagnostic(
        MODE_DISALLOWED_CODE,
        "committed entry mode is not allowed by policy",
        "scripts/tool.sh",
        "100755",
        ModeKind.EXECUTABLE,
        (ModeKind.REGULAR,),
        "**/*.sh",
    )
    with pytest.raises(ValueError, match="complete exact ordered"):
        ModeReport(first_match_policy, selection, (forged,))


def test_mode_report_exposes_exact_counts_and_cached_decisions(tmp_path: Path) -> None:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    selection = _selection(
        tmp_path,
        _entry("a"),
        _entry("b", mode="100755", oid="c" * 40),
        _entry("link", mode="120000", oid="d" * 40),
        _entry("vendor", mode="160000", object_type="commit", oid="e" * 40),
    )

    report = check_modes(policy, selection)

    assert report.status == "failed"
    assert not report.valid
    assert report.finding_count == 3
    assert report.entries_by_mode == {
        "regular": 1,
        "executable": 1,
        "symlink": 1,
        "gitlink": 1,
    }
    assert report._selected_modes_for_path("a") == ((ModeKind.REGULAR,), None)
    with pytest.raises(ValueError, match="does not identify"):
        report._selected_modes_for_path("missing")
