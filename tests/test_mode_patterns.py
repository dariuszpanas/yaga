from __future__ import annotations

from typing import Any, cast

import pytest

from yaga.modes.patterns import (
    ModeMatchWorkLimitError,
    _compile_mode_components,
    _match_compiled_mode_components,
    _MatchWork,
    match_mode_pattern,
    validate_mode_pattern,
)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("README.md", "README.md", True),
        ("README.md", "readme.md", False),
        ("*.sh", "tool.sh", True),
        ("*.sh", "scripts/tool.sh", False),
        ("scripts/*/tool", "scripts/bin/tool", True),
        ("scripts/*/tool", "scripts/a/b/tool", False),
        ("vendor/**", "vendor", True),
        ("vendor/**", "vendor/deep/module", True),
        ("**/*.sh", "tool.sh", True),
        ("**/*.sh", "scripts/deep/tool.sh", True),
        ("literal[?]", "literal[?]", True),
        ("aux:?.txt", "aux:?.txt", True),
    ],
)
def test_match_mode_pattern_uses_anchored_case_sensitive_component_semantics(
    pattern: str,
    path: str,
    expected: bool,
) -> None:
    assert match_mode_pattern(pattern, path) is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "/absolute",
        "C:/drive",
        "dir\\file",
        "./file",
        "dir/../file",
        "dir//file",
        "dir/**suffix",
        "line\nbreak",
        "bidi\u202efile",
        "surrogate\ud800",
    ],
)
def test_validate_mode_pattern_rejects_nonportable_or_ambiguous_values(
    pattern: str,
) -> None:
    with pytest.raises(ValueError, match="mode pattern"):
        validate_mode_pattern(pattern)


def test_match_mode_pattern_accepts_windows_invalid_committed_path_for_checking() -> None:
    assert match_mode_pattern("**", "dir/literal\\backslash")
    assert match_mode_pattern("**", "aux?.txt")


def test_mode_matcher_enforces_one_supplied_work_budget() -> None:
    compiled = _compile_mode_components(validate_mode_pattern("**"))
    work = _MatchWork(limit=0)

    with pytest.raises(ModeMatchWorkLimitError):
        _match_compiled_mode_components(compiled, ("path",), spend_work=work.spend)


@pytest.mark.parametrize("limit", [True, -1, 1.5, "1"])
def test_mode_match_work_rejects_invalid_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        _MatchWork(cast(Any, limit))
