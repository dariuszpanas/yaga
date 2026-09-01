from __future__ import annotations

from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.paths.models import (
    ASCII_CASE_COLLISION_RULE,
    MAX_PATH_POLICY_RULES,
    PATH_RULES,
    WINDOWS_CHARACTERS_RULE,
    WINDOWS_COMPATIBLE_V1_PROFILE,
)
from yaga.paths.policy import MAX_PATH_POLICY_BYTES, load_path_policy


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_path_policy_expands_fixed_profile_and_resolves_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "paths.toml"
    path.write_bytes(b"\xef\xbb\xbfpath-policy-version = 1\nprofile = 'windows-compatible-v1'\n")
    monkeypatch.chdir(tmp_path)

    loaded = load_path_policy(Path("paths.toml"))

    assert loaded.path == path.resolve()
    assert loaded.policy.profile == WINDOWS_COMPATIBLE_V1_PROFILE
    assert loaded.policy.rules == PATH_RULES


def test_load_path_policy_accepts_custom_subset_and_normalizes_fixed_order(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "paths.toml",
        "path-policy-version = 1\nrules = ['ascii-case-collision', 'windows-characters']\n",
    )

    policy = load_path_policy(path).policy

    assert policy.profile is None
    assert policy.rules == (WINDOWS_CHARACTERS_RULE, ASCII_CASE_COLLISION_RULE)


@pytest.mark.parametrize(
    "document",
    [
        "profile = 'windows-compatible-v1'\n",
        "path-policy-version = 2\nprofile = 'windows-compatible-v1'\n",
        "path-policy-version = true\nprofile = 'windows-compatible-v1'\n",
        "path-policy-version = 1\n",
        "path-policy-version = 1\nprofile = 'windows-compatible-v1'\nrules = ['windows-trailing']\n",
        "path-policy-version = 1\nprofile = 'unknown'\n",
        "path-policy-version = 1\nprofile = true\n",
        "path-policy-version = 1\nrules = []\n",
        "path-policy-version = 1\nrules = 'windows-trailing'\n",
        "path-policy-version = 1\nrules = [true]\n",
        "path-policy-version = 1\nrules = ['windows-trailing', 'windows-trailing']\n",
        "path-policy-version = 1\nrules = ['unknown']\n",
        "path-policy-version = 1\nrules = ['windows-trailing']\nextra = 1\n",
    ],
)
def test_load_path_policy_rejects_closed_schema_and_invalid_values(
    tmp_path: Path,
    document: str,
) -> None:
    with pytest.raises(ConfigurationError):
        load_path_policy(_write(tmp_path / "paths.toml", document))


def test_load_path_policy_rejects_rule_and_file_bounds(tmp_path: Path) -> None:
    too_many_rules = ", ".join(f"'rule-{index}'" for index in range(MAX_PATH_POLICY_RULES + 1))
    too_many = _write(
        tmp_path / "many.toml",
        f"path-policy-version = 1\nrules = [{too_many_rules}]\n",
    )
    oversized = tmp_path / "oversized.toml"
    oversized.write_bytes(b"#" * (MAX_PATH_POLICY_BYTES + 1))

    with pytest.raises(ConfigurationError, match="1 through 4"):
        load_path_policy(too_many)
    with pytest.raises(ConfigurationError, match="exceeds"):
        load_path_policy(oversized)


def test_load_path_policy_rejects_missing_invalid_and_non_utf8_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read"):
        load_path_policy(tmp_path / "missing.toml")
    with pytest.raises(ConfigurationError, match="invalid path policy TOML"):
        load_path_policy(_write(tmp_path / "invalid.toml", "not = [valid"))
    invalid_utf8 = tmp_path / "invalid-utf8.toml"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="valid UTF-8"):
        load_path_policy(invalid_utf8)
