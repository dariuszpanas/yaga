"""Tests for strict explicit branch-policy loading."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from yaga.branches.models import MAX_BRANCH_PATTERNS, BranchPolicy, LoadedBranchPolicy
from yaga.branches.policy import MAX_BRANCH_POLICY_BYTES, load_branch_policy
from yaga.errors import ConfigurationError


def write_policy(directory: Path, body: str | bytes) -> Path:
    path = directory / "branch-policy.toml"
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


def policy_document(patterns: Sequence[object] | None = None) -> str:
    values = ["main", "feat/*"] if patterns is None else patterns
    return (
        f"branch-policy-version = 1\nallowed-patterns = {json.dumps(values, ensure_ascii=False)}\n"
    )


def test_minimal_explicit_policy_loads_with_resolved_path_and_preserved_order(
    tmp_path: Path,
) -> None:
    path = write_policy(tmp_path, policy_document(["main", "feat/**", "Feat/*"]))

    loaded = load_branch_policy(path)

    assert loaded == LoadedBranchPolicy(
        policy=BranchPolicy(1, ("main", "feat/**", "Feat/*")),
        path=path.resolve(),
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('allowed-patterns = ["main"]\n', "missing required key.*branch-policy-version"),
        ("branch-policy-version = 1\n", "missing required key.*allowed-patterns"),
        ('branch-policy-version = 2\nallowed-patterns = ["main"]\n', "integer 1"),
        ('branch-policy-version = true\nallowed-patterns = ["main"]\n', "integer 1"),
        ('branch-policy-version = "1"\nallowed-patterns = ["main"]\n', "integer 1"),
        ('branch-policy-version = 1\nallowed-patterns = "main"\n', "array of strings"),
        ("branch-policy-version = 1\nallowed-patterns = [1]\n", "array of strings"),
        ("branch-policy-version = 1\nallowed-patterns = []\n", "must not be empty"),
    ],
)
def test_required_root_contract_is_strict(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_branch_policy(write_policy(tmp_path, body))


@pytest.mark.parametrize(
    "key",
    ["name", "repo", "event", "environment", "include", "expression", "command", "format"],
)
def test_unknown_runtime_or_expansion_keys_are_rejected(tmp_path: Path, key: str) -> None:
    body = policy_document() + f'{key} = "unsupported"\n'

    with pytest.raises(ConfigurationError, match=rf"unknown branch policy key.*{key}"):
        load_branch_policy(write_policy(tmp_path, body))


def test_pattern_collection_is_bounded_exactly_unique_and_case_sensitive(
    tmp_path: Path,
) -> None:
    exact = [f"branch-{index}" for index in range(MAX_BRANCH_PATTERNS)]
    assert (
        len(
            load_branch_policy(
                write_policy(tmp_path, policy_document(exact))
            ).policy.allowed_patterns
        )
        == MAX_BRANCH_PATTERNS
    )

    too_many = exact + ["overflow"]
    with pytest.raises(ConfigurationError, match=rf"exceeds {MAX_BRANCH_PATTERNS}"):
        load_branch_policy(write_policy(tmp_path, policy_document(too_many)))
    with pytest.raises(ConfigurationError, match="must be unique"):
        load_branch_policy(write_policy(tmp_path, policy_document(["main", "main"])))

    distinct = load_branch_policy(write_policy(tmp_path, policy_document(["feat/*", "Feat/*"])))
    assert distinct.policy.allowed_patterns == ("feat/*", "Feat/*")


@pytest.mark.parametrize(
    "pattern",
    ["bad pattern", "é", "feat/**x", "1*", "feat/x.lock", "a" * 245],
)
def test_invalid_patterns_are_configuration_errors(tmp_path: Path, pattern: str) -> None:
    with pytest.raises(ConfigurationError, match="invalid branch policy.*branch pattern"):
        load_branch_policy(write_policy(tmp_path, policy_document([pattern])))


def test_policy_size_limit_is_inclusive_and_detects_one_extra_byte(tmp_path: Path) -> None:
    prefix = policy_document(["main"]).encode() + b"#"
    exact = prefix + b"x" * (MAX_BRANCH_POLICY_BYTES - len(prefix))
    path = write_policy(tmp_path, exact)

    assert load_branch_policy(path).policy.allowed_patterns == ("main",)

    path.write_bytes(exact + b"x")
    with pytest.raises(ConfigurationError, match=rf"exceeds {MAX_BRANCH_POLICY_BYTES} bytes"):
        load_branch_policy(path)


def test_invalid_utf8_invalid_toml_missing_file_and_utf8_bom_boundaries(
    tmp_path: Path,
) -> None:
    path = write_policy(tmp_path, b"\xff")
    with pytest.raises(ConfigurationError, match="not valid UTF-8"):
        load_branch_policy(path)

    path.write_text("branch-policy-version = [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid branch policy TOML"):
        load_branch_policy(path)

    with pytest.raises(ConfigurationError, match="cannot read branch policy"):
        load_branch_policy(tmp_path / "missing.toml")

    bom = b"\xef\xbb\xbf" + policy_document(["main"]).encode()
    assert load_branch_policy(write_policy(tmp_path, bom)).policy.allowed_patterns == ("main",)
