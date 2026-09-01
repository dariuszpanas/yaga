from __future__ import annotations

from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.trees.models import MAX_TREE_POLICY_ENTRIES
from yaga.trees.policy import MAX_TREE_POLICY_BYTES, load_tree_policy


def _write_policy(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_tree_policy_preserves_order_and_resolves_explicit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "tree.toml"
    policy_path.write_bytes(
        b"\xef\xbb\xbf"
        b"tree-policy-version = 1\n"
        b'required-paths = ["pyproject.toml", "README.md"]\n'
        b'forbidden-patterns = ["*.pem", "vendor/**"]\n'
    )
    monkeypatch.chdir(tmp_path)

    loaded = load_tree_policy(Path("tree.toml"))

    assert loaded.path == policy_path.resolve()
    assert loaded.policy.required_paths == ("pyproject.toml", "README.md")
    assert loaded.policy.forbidden_patterns == ("*.pem", "vendor/**")


@pytest.mark.parametrize(
    "document",
    [
        "required-paths = ['README.md']\nforbidden-patterns = []\n",
        "tree-policy-version = 2\nrequired-paths = ['README.md']\nforbidden-patterns = []\n",
        "tree-policy-version = true\nrequired-paths = ['README.md']\nforbidden-patterns = []\n",
        "tree-policy-version = 1\nrequired-paths = 'README.md'\nforbidden-patterns = []\n",
        "tree-policy-version = 1\nrequired-paths = []\nforbidden-patterns = []\n",
        "tree-policy-version = 1\nrequired-paths = ['x', 'x']\nforbidden-patterns = []\n",
        "tree-policy-version = 1\nrequired-paths = ['x']\nforbidden-patterns = []\nextra = 1\n",
        "tree-policy-version = 1\nrequired-paths = ['secret.pem']\nforbidden-patterns = ['*.pem']\n",
        "tree-policy-version = 1\nrequired-paths = ['/absolute']\nforbidden-patterns = []\n",
        "tree-policy-version = 1\nrequired-paths = []\nforbidden-patterns = ['a/**x']\n",
    ],
)
def test_load_tree_policy_rejects_closed_schema_and_impossible_values(
    tmp_path: Path,
    document: str,
) -> None:
    path = _write_policy(tmp_path / "tree.toml", document)

    with pytest.raises(ConfigurationError):
        load_tree_policy(path)


def test_load_tree_policy_allows_either_array_to_be_empty(tmp_path: Path) -> None:
    required_only = _write_policy(
        tmp_path / "required.toml",
        "tree-policy-version = 1\nrequired-paths = ['README.md']\nforbidden-patterns = []\n",
    )
    forbidden_only = _write_policy(
        tmp_path / "forbidden.toml",
        "tree-policy-version = 1\nrequired-paths = []\nforbidden-patterns = ['vendor/**']\n",
    )

    assert load_tree_policy(required_only).policy.required_paths == ("README.md",)
    assert load_tree_policy(forbidden_only).policy.forbidden_patterns == ("vendor/**",)


def test_load_tree_policy_rejects_combined_entry_overflow(tmp_path: Path) -> None:
    required = ", ".join(f"'required-{index}'" for index in range(MAX_TREE_POLICY_ENTRIES))
    path = _write_policy(
        tmp_path / "tree.toml",
        "tree-policy-version = 1\n"
        f"required-paths = [{required}]\n"
        "forbidden-patterns = ['forbidden']\n",
    )

    with pytest.raises(ConfigurationError, match="combined entries"):
        load_tree_policy(path)


def test_load_tree_policy_rejects_oversized_and_non_utf8_files(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.toml"
    oversized.write_bytes(b"#" * (MAX_TREE_POLICY_BYTES + 1))
    invalid_utf8 = tmp_path / "invalid.toml"
    invalid_utf8.write_bytes(b"\xff")

    with pytest.raises(ConfigurationError, match="exceeds"):
        load_tree_policy(oversized)
    with pytest.raises(ConfigurationError, match="valid UTF-8"):
        load_tree_policy(invalid_utf8)


def test_load_tree_policy_rejects_missing_and_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read"):
        load_tree_policy(tmp_path / "missing.toml")
    invalid = _write_policy(tmp_path / "invalid.toml", "not = [valid")
    with pytest.raises(ConfigurationError, match="invalid tree policy TOML"):
        load_tree_policy(invalid)
