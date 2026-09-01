from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import InputError
from yaga.modes import checker
from yaga.modes.checker import check_modes
from yaga.modes.models import (
    MODE_DISALLOWED_CODE,
    ModeEntry,
    ModeKind,
    ModePathOverride,
    ModePolicy,
    ModeSelection,
)


def _entry(
    path: str,
    mode: str,
    *,
    oid: str,
    object_type: str = "blob",
) -> ModeEntry:
    return ModeEntry(path, oid, mode, object_type)


def _selection(tmp_path: Path, *entries: ModeEntry) -> ModeSelection:
    return ModeSelection(
        repository=tmp_path.resolve(),
        revision="HEAD",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        entries=tuple(sorted(entries, key=lambda item: item.path)),
    )


def test_check_modes_applies_first_override_and_orders_findings_lexically(
    tmp_path: Path,
) -> None:
    policy = ModePolicy(
        1,
        (ModeKind.REGULAR,),
        (
            ModePathOverride("scripts/**", (ModeKind.EXECUTABLE,)),
            ModePathOverride("scripts/*.sh", (ModeKind.REGULAR,)),
            ModePathOverride("vendor/**", (ModeKind.GITLINK,)),
        ),
    )
    selection = _selection(
        tmp_path,
        _entry("a-link", "120000", oid="c" * 40),
        _entry("scripts/app.sh", "100755", oid="d" * 40),
        _entry("scripts/plain", "100644", oid="e" * 40),
        _entry("vendor/module", "160000", object_type="commit", oid="f" * 40),
    )

    report = check_modes(policy, selection)

    assert [
        (
            item.code,
            item.path,
            item.actual_mode,
            item.actual_kind,
            item.allowed_modes,
            item.pattern,
        )
        for item in report.diagnostics
    ] == [
        (
            MODE_DISALLOWED_CODE,
            "a-link",
            "120000",
            ModeKind.SYMLINK,
            (ModeKind.REGULAR,),
            None,
        ),
        (
            MODE_DISALLOWED_CODE,
            "scripts/plain",
            "100644",
            ModeKind.REGULAR,
            (ModeKind.EXECUTABLE,),
            "scripts/**",
        ),
    ]


def test_check_modes_handles_all_closed_kinds_and_equality(tmp_path: Path) -> None:
    policy = ModePolicy(1, tuple(ModeKind))
    selection = _selection(
        tmp_path,
        _entry("regular", "100644", oid="c" * 40),
        _entry("executable", "100755", oid="d" * 40),
        _entry("symlink", "120000", oid="e" * 40),
        _entry("gitlink", "160000", object_type="commit", oid="f" * 40),
    )

    report = check_modes(policy, selection)

    assert report.valid
    assert report.finding_count == 0


def test_check_modes_matches_paths_case_sensitively(tmp_path: Path) -> None:
    policy = ModePolicy(
        1,
        (ModeKind.REGULAR,),
        (ModePathOverride("Scripts/**", (ModeKind.EXECUTABLE,)),),
    )
    selection = _selection(
        tmp_path,
        _entry("scripts/tool", "100755", oid="c" * 40),
    )

    report = check_modes(policy, selection)

    assert report.diagnostics[0].pattern is None


def test_check_modes_bounds_stored_diagnostics_but_preserves_exact_count(
    tmp_path: Path,
) -> None:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    selection = _selection(
        tmp_path,
        *(_entry(f"scripts/{index:04d}", "100755", oid="c" * 40) for index in range(300)),
    )

    report = check_modes(policy, selection)

    assert report.finding_count == 300
    assert len(report.diagnostics) == 256
    assert report.diagnostics_omitted == 44
    assert report.diagnostics[0].path == "scripts/0000"
    assert report.diagnostics[-1].path == "scripts/0255"

    from yaga.modes.models import ModeReport

    with pytest.raises(ValueError, match="complete exact ordered"):
        ModeReport(policy, selection, report.diagnostics[:-1])


def test_check_modes_maps_match_work_exhaustion_to_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ModePolicy(
        1,
        (ModeKind.REGULAR,),
        (ModePathOverride("**", (ModeKind.REGULAR,)),),
    )
    selection = _selection(tmp_path, _entry("file", "100644", oid="c" * 40))
    monkeypatch.setattr(checker, "MAX_MODE_MATCH_WORK", 0)

    with pytest.raises(InputError, match="0-unit match-work limit"):
        check_modes(policy, selection)


def test_check_modes_uses_one_policy_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    selection = _selection(tmp_path, _entry("file", "100644", oid="c" * 40))
    original = checker._evaluate_mode_policy
    calls = 0

    def counted_evaluation(
        selected_policy: ModePolicy,
        selected_tree: ModeSelection,
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

    monkeypatch.setattr(checker, "_evaluate_mode_policy", counted_evaluation)

    assert check_modes(policy, selection).valid
    assert calls == 1


def test_check_modes_rejects_wrong_model_types(tmp_path: Path) -> None:
    policy = ModePolicy(1, (ModeKind.REGULAR,))
    selection = _selection(tmp_path)

    with pytest.raises(TypeError, match="ModePolicy"):
        check_modes(cast(Any, object()), selection)
    with pytest.raises(TypeError, match="ModeSelection"):
        check_modes(policy, cast(Any, object()))
