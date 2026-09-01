from __future__ import annotations

import pytest

from yaga.sizes.patterns import (
    SizeMatchWorkLimitError,
    _compile_size_components,
    _match_compiled_size_components,
    _MatchWork,
    match_size_pattern,
    validate_size_pattern,
)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("README.md", "README.md", True),
        ("README.md", "readme.md", False),
        ("*.bin", "artifact.bin", True),
        ("*.bin", "nested/artifact.bin", False),
        ("assets/*/bundle.js", "assets/web/bundle.js", True),
        ("assets/*/bundle.js", "assets/web/deep/bundle.js", False),
        ("vendor/**", "vendor", True),
        ("vendor/**", "vendor/deep/file", True),
        ("**/large/*", "src/large/file", True),
        ("literal[?]", "literal[?]", True),
    ],
)
def test_match_size_pattern_uses_canonical_component_semantics(
    pattern: str,
    path: str,
    expected: bool,
) -> None:
    assert match_size_pattern(pattern, path) is expected


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
def test_validate_size_pattern_uses_size_specific_failure(pattern: str) -> None:
    with pytest.raises(ValueError, match="size pattern"):
        validate_size_pattern(pattern)


def test_size_matcher_enforces_one_supplied_work_budget() -> None:
    compiled = _compile_size_components(validate_size_pattern("**"))
    work = _MatchWork(limit=0)

    with pytest.raises(SizeMatchWorkLimitError):
        _match_compiled_size_components(compiled, ("path",), spend_work=work.spend)
