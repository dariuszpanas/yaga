from __future__ import annotations

from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.sizes.models import MAX_SIZE_BYTES, MAX_SIZE_POLICY_ENTRIES
from yaga.sizes.policy import MAX_SIZE_POLICY_BYTES, load_size_policy


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_size_policy_accepts_minimal_default_only_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "size.toml"
    path.write_bytes(b"\xef\xbb\xbfsize-policy-version = 1\ndefault-max-blob-bytes = 128\n")
    monkeypatch.chdir(tmp_path)

    loaded = load_size_policy(Path("size.toml"))

    assert loaded.path == path.resolve()
    assert loaded.policy.default_max_blob_bytes == 128
    assert loaded.policy.max_total_blob_bytes is None
    assert loaded.policy.path_limits == ()


def test_load_size_policy_preserves_ordered_first_match_limits(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "size.toml",
        "size-policy-version = 1\n"
        "default-max-blob-bytes = 128\n"
        "max-total-blob-bytes = 4096\n"
        "[[path-limits]]\npattern = 'assets/**'\nmax-blob-bytes = 2048\n"
        "[[path-limits]]\npattern = 'assets/*.map'\nmax-blob-bytes = 1024\n",
    )

    policy = load_size_policy(path).policy

    assert [(item.pattern, item.max_blob_bytes) for item in policy.path_limits] == [
        ("assets/**", 2048),
        ("assets/*.map", 1024),
    ]


@pytest.mark.parametrize(
    "document",
    [
        "default-max-blob-bytes = 1\n",
        "size-policy-version = 2\ndefault-max-blob-bytes = 1\n",
        "size-policy-version = true\ndefault-max-blob-bytes = 1\n",
        "size-policy-version = 1\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = true\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = -1\n",
        f"size-policy-version = 1\ndefault-max-blob-bytes = {MAX_SIZE_BYTES + 1}\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = 1\nextra = 1\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = 1\npath-limits = 'x'\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = 1\n[[path-limits]]\npattern = 'x'\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = 1\n"
        "[[path-limits]]\npattern = 'x'\nmax-blob-bytes = 1\nextra = 2\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = 1\n"
        "[[path-limits]]\npattern = 'bad/**suffix'\nmax-blob-bytes = 1\n",
        "size-policy-version = 1\ndefault-max-blob-bytes = 1\n"
        "[[path-limits]]\npattern = 'x'\nmax-blob-bytes = 1\n"
        "[[path-limits]]\npattern = 'x'\nmax-blob-bytes = 2\n",
    ],
)
def test_load_size_policy_rejects_closed_schema_and_invalid_values(
    tmp_path: Path,
    document: str,
) -> None:
    with pytest.raises(ConfigurationError):
        load_size_policy(_write(tmp_path / "size.toml", document))


def test_load_size_policy_rejects_entry_and_file_bounds(tmp_path: Path) -> None:
    entries = "\n".join(
        f"[[path-limits]]\npattern = 'p{index}'\nmax-blob-bytes = 1"
        for index in range(MAX_SIZE_POLICY_ENTRIES + 1)
    )
    too_many = _write(
        tmp_path / "many.toml",
        f"size-policy-version = 1\ndefault-max-blob-bytes = 1\n{entries}\n",
    )
    oversized = tmp_path / "oversized.toml"
    oversized.write_bytes(b"#" * (MAX_SIZE_POLICY_BYTES + 1))

    with pytest.raises(ConfigurationError, match="exceeds 128"):
        load_size_policy(too_many)
    with pytest.raises(ConfigurationError, match="exceeds"):
        load_size_policy(oversized)


def test_load_size_policy_rejects_missing_invalid_and_non_utf8_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="cannot read"):
        load_size_policy(tmp_path / "missing.toml")
    with pytest.raises(ConfigurationError, match="invalid size policy TOML"):
        load_size_policy(_write(tmp_path / "invalid.toml", "not = [valid"))
    invalid_utf8 = tmp_path / "invalid-utf8.toml"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="valid UTF-8"):
        load_size_policy(invalid_utf8)
