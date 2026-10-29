"""Strict explicit description policy boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.descriptions.models import (
    DescriptionDiagnostic,
    DescriptionPolicy,
    DescriptionReport,
    LoadedDescriptionPolicy,
)
from yaga.descriptions.policy import load_description_policy
from yaga.errors import ConfigurationError


def _policy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "description.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_and_explicit_policy_path(tmp_path: Path) -> None:
    path = _policy(tmp_path, "description-policy-version = 1\n")
    loaded = load_description_policy(path)
    assert loaded.path == path.resolve()
    assert loaded.policy == DescriptionPolicy()


def test_every_option_round_trips_without_discovery(tmp_path: Path) -> None:
    path = _policy(
        tmp_path,
        """description-policy-version = 1
min-words = 5
max-length = 200
placeholders = ["Replace this", "Not yet"]
require-checked-tasks = true
typos = "check"
heading-min-level = 1
heading-max-length = 80
""",
    )
    (tmp_path / ".yaga.toml").write_text("invalid", encoding="utf-8")
    assert load_description_policy(path).policy == DescriptionPolicy(
        min_words=5,
        max_length=200,
        placeholders=("Replace this", "Not yet"),
        require_checked_tasks=True,
        typos="check",
        heading_min_level=1,
        heading_max_length=80,
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "description-policy-version = 2",
        "description-policy-version = true",
        'description-policy-version = "1"',
        "description-policy-version = 1.0",
        "description-policy-version = 1\nunknown = true",
        "description-policy-version = 1\n[description]\nmin-words = 2",
        "description-policy-version = 1\nmin-words = 1\nmin-words = 2",
        "description-policy-version = 1\nmin-words = -1",
        "description-policy-version = 1\nmin-words = 65537",
        "description-policy-version = 1\nmin-words = true",
        "description-policy-version = 1\nmin-words = 1.0",
        "description-policy-version = 1\nmax-length = 0",
        "description-policy-version = 1\nmax-length = 65537",
        "description-policy-version = 1\nmax-length = false",
        "description-policy-version = 1\nmin-words = 3\nmax-length = 4",
        'description-policy-version = 1\nplaceholders = "TODO"',
        "description-policy-version = 1\nplaceholders = [1]",
        'description-policy-version = 1\nplaceholders = [""]',
        'description-policy-version = 1\nplaceholders = ["TODO", "todo"]',
        'description-policy-version = 1\nplaceholders = [" trim"]',
        'description-policy-version = 1\nplaceholders = ["line\\nline"]',
        'description-policy-version = 1\nplaceholders = ["\\u202Ehidden"]',
        "description-policy-version = 1\nrequire-checked-tasks = 1",
        'description-policy-version = 1\nrequire-checked-tasks = "true"',
        'description-policy-version = 1\ntypos = "yes"',
        "description-policy-version = 1\ntypos = true",
        "description-policy-version = 1\ntypos = []",
        "[broken",
    ],
)
def test_rejects_malformed_and_ambiguous_policy(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigurationError):
        load_description_policy(_policy(tmp_path, text))


def test_empty_placeholders_and_zero_minimum_are_explicitly_allowed(tmp_path: Path) -> None:
    loaded = load_description_policy(
        _policy(tmp_path, "description-policy-version=1\nmin-words=0\nplaceholders=[]")
    )
    assert loaded.policy.placeholders == ()
    assert loaded.policy.min_words == 0


@pytest.mark.parametrize("raw", [b"\xff", b"#" * 65537], ids=["invalid-utf8", "over-limit"])
def test_policy_encoding_and_byte_limits(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "description.toml"
    path.write_bytes(raw)
    with pytest.raises(ConfigurationError):
        load_description_policy(path)


def test_policy_missing_or_directory_is_an_expected_error(tmp_path: Path) -> None:
    for path in (tmp_path / "missing", tmp_path):
        with pytest.raises(ConfigurationError):
            load_description_policy(path)


def test_policy_limits_placeholder_count_and_length(tmp_path: Path) -> None:
    for placeholders in (("x" * 129,), tuple(str(i) for i in range(129))):
        with pytest.raises(ValueError):
            DescriptionPolicy(placeholders=placeholders)


def test_report_rejects_inconsistent_counts_and_unrecognized_codes() -> None:
    with pytest.raises(ValueError):
        DescriptionDiagnostic("invented", "message")
    with pytest.raises(ValueError):
        DescriptionDiagnostic("description.required", "message", line=True)
    with pytest.raises(ValueError):
        DescriptionReport(DescriptionPolicy(), (), 1, 0, 0)
    with pytest.raises(ValueError):
        LoadedDescriptionPolicy(DescriptionPolicy(), Path("relative"))


def test_bom_policy_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "description.toml"
    path.write_bytes(b"\xef\xbb\xbfdescription-policy-version=1")
    assert load_description_policy(path).policy == DescriptionPolicy()
