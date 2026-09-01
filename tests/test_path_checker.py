from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import InputError
from yaga.paths import checker
from yaga.paths.checker import check_paths
from yaga.paths.models import (
    ASCII_CASE_COLLISION_RULE,
    PATH_ASCII_CASE_COLLISION_CODE,
    PATH_RULES,
    PATH_WINDOWS_CHARACTER_CODE,
    PATH_WINDOWS_RESERVED_CODE,
    PATH_WINDOWS_TRAILING_CODE,
    WINDOWS_CHARACTERS_RULE,
    WINDOWS_RESERVED_RULE,
    WINDOWS_TRAILING_RULE,
    PathPolicy,
    PathSelection,
)


def _policy(*rules: str) -> PathPolicy:
    return PathPolicy(1, tuple(rules))


def _selection(repository: Path, *paths: str) -> PathSelection:
    return PathSelection(
        repository=repository.resolve(),
        revision="HEAD",
        commit_sha="a" * 40,
        tree_sha="b" * 40,
        paths=tuple(sorted(paths)),
    )


def test_check_paths_passes_portable_names(tmp_path: Path) -> None:
    selection = _selection(
        tmp_path,
        ".github/workflows/ci.yml",
        "README.md",
        "src/yaga/checker.py",
        "tests/test_checker.py",
    )

    report = check_paths(_policy(*PATH_RULES), selection)

    assert report.valid
    assert report.status == "passed"
    assert report.diagnostics == ()
    assert report.finding_count == 0
    assert report.findings_by_rule == dict.fromkeys(PATH_RULES, 0)


@pytest.mark.parametrize("character", tuple('<>:"\\|?*') + ("\x01", "\x1f"))
def test_check_paths_rejects_each_windows_character_once(
    tmp_path: Path,
    character: str,
) -> None:
    selection = _selection(tmp_path, f"safe/first{character}second{character}")

    report = check_paths(_policy(WINDOWS_CHARACTERS_RULE), selection)

    assert report.finding_count == 1
    assert [
        (item.code, item.path, item.component, item.related_path, item.message)
        for item in report.diagnostics
    ] == [
        (
            PATH_WINDOWS_CHARACTER_CODE,
            f"safe/first{character}second{character}",
            2,
            None,
            f"path component contains Windows-incompatible character U+{ord(character):04X}",
        )
    ]


@pytest.mark.parametrize(
    ("character", "identity"),
    (("\\", "U+005C"), ("\x1b", "U+001B")),
)
def test_check_paths_reports_sanitized_windows_character_identity(
    tmp_path: Path,
    character: str,
    identity: str,
) -> None:
    report = check_paths(
        _policy(WINDOWS_CHARACTERS_RULE),
        _selection(tmp_path, f"name{character}part"),
    )

    assert report.diagnostics[0].message.endswith(identity)
    assert character not in report.diagnostics[0].message


def test_check_paths_uses_first_component_for_each_component_local_rule(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path, "bad:/also?/con.txt/NUL./tail ")

    report = check_paths(
        _policy(
            WINDOWS_CHARACTERS_RULE,
            WINDOWS_TRAILING_RULE,
            WINDOWS_RESERVED_RULE,
        ),
        selection,
    )

    assert [(item.code, item.component) for item in report.diagnostics] == [
        (PATH_WINDOWS_CHARACTER_CODE, 1),
        (PATH_WINDOWS_TRAILING_CODE, 4),
        (PATH_WINDOWS_RESERVED_CODE, 3),
    ]
    assert report.findings_by_rule == {
        WINDOWS_CHARACTERS_RULE: 1,
        WINDOWS_TRAILING_RULE: 1,
        WINDOWS_RESERVED_RULE: 1,
    }


@pytest.mark.parametrize("suffix", (" ", "."))
def test_check_paths_rejects_trailing_ascii_space_or_period(
    tmp_path: Path,
    suffix: str,
) -> None:
    report = check_paths(
        _policy(WINDOWS_TRAILING_RULE),
        _selection(tmp_path, f"safe/name{suffix}"),
    )

    assert [(item.code, item.component) for item in report.diagnostics] == [
        (PATH_WINDOWS_TRAILING_CODE, 2)
    ]


@pytest.mark.parametrize(
    "component",
    (
        "CON",
        "prn.txt",
        "AuX.any.extension",
        "nul",
        "COM1",
        "com9.log",
        "LPT1",
        "lPt9.log",
        "COM¹.txt",
        "com²",
        "COM³.log",
        "LPT¹.txt",
        "lpt²",
        "LPT³.log",
    ),
)
def test_check_paths_rejects_windows_device_basename(
    tmp_path: Path,
    component: str,
) -> None:
    report = check_paths(
        _policy(WINDOWS_RESERVED_RULE),
        _selection(tmp_path, f"safe/{component}"),
    )

    assert [(item.code, item.component) for item in report.diagnostics] == [
        (PATH_WINDOWS_RESERVED_CODE, 2)
    ]


@pytest.mark.parametrize(
    "component",
    (
        ".CON",
        "CONSOLE",
        "COM0",
        "COM10",
        "LPT0",
        "LPT10",
        "CÖN",
        "com⁴",
    ),
)
def test_check_paths_allows_names_outside_exact_device_basename_set(
    tmp_path: Path,
    component: str,
) -> None:
    assert check_paths(
        _policy(WINDOWS_RESERVED_RULE),
        _selection(tmp_path, f"safe/{component}"),
    ).valid


def test_check_paths_reports_every_full_leaf_alias_with_first_other(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path, "AB", "Ab", "aB", "ab")

    report = check_paths(_policy(ASCII_CASE_COLLISION_RULE), selection)

    assert [(item.path, item.related_path) for item in report.diagnostics] == [
        ("AB", "Ab"),
        ("Ab", "AB"),
        ("aB", "AB"),
        ("ab", "AB"),
    ]
    assert all(item.code == PATH_ASCII_CASE_COLLISION_CODE for item in report.diagnostics)


def test_check_paths_catches_file_directory_alias_across_interposed_sibling(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path, "A", "B/interposed", "a/child")

    report = check_paths(_policy(ASCII_CASE_COLLISION_RULE), selection)

    assert [(item.path, item.related_path) for item in report.diagnostics] == [
        ("A", "a/child"),
        ("a/child", "A"),
    ]


def test_check_paths_catches_reverse_lexical_file_directory_alias(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path, "A/child", "a")

    report = check_paths(_policy(ASCII_CASE_COLLISION_RULE), selection)

    assert [(item.path, item.related_path) for item in report.diagnostics] == [
        ("A/child", "a"),
        ("a", "A/child"),
    ]


def test_check_paths_chooses_first_related_across_leaf_and_prefix_aliases(
    tmp_path: Path,
) -> None:
    selection = _selection(tmp_path, "AB", "Ab/child", "ab")

    report = check_paths(_policy(ASCII_CASE_COLLISION_RULE), selection)

    assert [(item.path, item.related_path) for item in report.diagnostics] == [
        ("AB", "Ab/child"),
        ("Ab/child", "AB"),
        ("ab", "AB"),
    ]


def test_check_paths_allows_directory_only_case_variants_and_nonprefixes(
    tmp_path: Path,
) -> None:
    selection = _selection(
        tmp_path,
        "Dir/a.txt",
        "Folderish/file.txt",
        "dir/b.txt",
        "folder",
    )

    assert check_paths(_policy(ASCII_CASE_COLLISION_RULE), selection).valid


def test_check_paths_uses_ascii_case_only_without_unicode_casefold(
    tmp_path: Path,
) -> None:
    selection = _selection(
        tmp_path,
        "S/file.txt",
        "Ä/file.txt",
        "ß/file.txt",
        "ä/file.txt",
        "ſ/file.txt",
    )

    assert check_paths(_policy(ASCII_CASE_COLLISION_RULE), selection).valid


def test_check_paths_orders_diagnostics_by_path_then_fixed_rule_order(
    tmp_path: Path,
) -> None:
    policy = _policy(*reversed(PATH_RULES))
    selection = _selection(tmp_path, "AUX. ", "aux. ")

    report = check_paths(policy, selection)

    assert [
        (item.path, item.code, item.component, item.related_path) for item in report.diagnostics
    ] == [
        ("AUX. ", PATH_WINDOWS_TRAILING_CODE, 1, None),
        ("AUX. ", PATH_WINDOWS_RESERVED_CODE, 1, None),
        ("AUX. ", PATH_ASCII_CASE_COLLISION_CODE, None, "aux. "),
        ("aux. ", PATH_WINDOWS_TRAILING_CODE, 1, None),
        ("aux. ", PATH_WINDOWS_RESERVED_CODE, 1, None),
        ("aux. ", PATH_ASCII_CASE_COLLISION_CODE, None, "AUX. "),
    ]
    assert tuple(item.rule for item in report.rule_counts) == PATH_RULES


def test_check_paths_caps_stored_diagnostics_but_keeps_exact_counts(
    tmp_path: Path,
) -> None:
    paths = tuple(f"bad:{index:03}" for index in range(300))

    report = check_paths(
        _policy(WINDOWS_CHARACTERS_RULE),
        _selection(tmp_path, *paths),
    )

    assert report.finding_count == 300
    assert report.findings_by_rule == {WINDOWS_CHARACTERS_RULE: 300}
    assert len(report.diagnostics) == 256
    assert report.diagnostics_omitted == 44
    assert report.diagnostics[0].path == "bad:000"
    assert report.diagnostics[-1].path == "bad:255"


def test_check_paths_maps_shared_work_exhaustion_to_input_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checker, "MAX_PATH_CHECK_WORK", 0)

    with pytest.raises(InputError, match="hard 0-unit work limit"):
        check_paths(
            _policy(WINDOWS_CHARACTERS_RULE),
            _selection(tmp_path, "README.md"),
        )


def test_check_paths_rejects_wrong_model_types(tmp_path: Path) -> None:
    policy = _policy(WINDOWS_CHARACTERS_RULE)
    selection = _selection(tmp_path, "README.md")

    with pytest.raises(TypeError, match="PathPolicy"):
        check_paths(cast(Any, object()), selection)
    with pytest.raises(TypeError, match="PathSelection"):
        check_paths(policy, cast(Any, object()))
