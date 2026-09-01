from __future__ import annotations

from pathlib import Path

import pytest

from yaga.errors import ConfigurationError
from yaga.modes.models import MAX_MODE_POLICY_OVERRIDES, ModeKind
from yaga.modes.policy import MAX_MODE_POLICY_BYTES, load_mode_policy


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_mode_policy_accepts_bom_and_canonicalizes_mode_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "mode.toml"
    path.write_bytes(
        b"\xef\xbb\xbfmode-policy-version = 1\ndefault-allowed-modes = ['gitlink', 'regular']\n"
    )
    monkeypatch.chdir(tmp_path)

    loaded = load_mode_policy(Path("mode.toml"))

    assert loaded.path == path.resolve()
    assert loaded.policy.default_allowed_modes == (
        ModeKind.REGULAR,
        ModeKind.GITLINK,
    )
    assert loaded.policy.path_overrides == ()


def test_load_mode_policy_preserves_ordered_first_match_overrides(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mode.toml",
        "mode-policy-version = 1\n"
        "default-allowed-modes = ['regular']\n"
        "[[path-overrides]]\n"
        "pattern = 'scripts/**'\n"
        "allowed-modes = ['symlink', 'executable']\n"
        "[[path-overrides]]\n"
        "pattern = '**/*.sh'\n"
        "allowed-modes = ['regular']\n",
    )

    policy = load_mode_policy(path).policy

    assert [(item.pattern, item.allowed_modes) for item in policy.path_overrides] == [
        ("scripts/**", (ModeKind.EXECUTABLE, ModeKind.SYMLINK)),
        ("**/*.sh", (ModeKind.REGULAR,)),
    ]


@pytest.mark.parametrize(
    "document",
    [
        "default-allowed-modes = ['regular']\n",
        "mode-policy-version = 2\ndefault-allowed-modes = ['regular']\n",
        "mode-policy-version = true\ndefault-allowed-modes = ['regular']\n",
        "mode-policy-version = 1\n",
        "mode-policy-version = 1\ndefault-allowed-modes = []\n",
        "mode-policy-version = 1\ndefault-allowed-modes = 'regular'\n",
        "mode-policy-version = 1\ndefault-allowed-modes = [1]\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular', 'regular']\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['tree']\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular']\nextra = 1\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular']\npath-overrides = 'x'\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n"
        "[[path-overrides]]\npattern = 'x'\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n"
        "[[path-overrides]]\npattern = 'x'\nallowed-modes = ['regular']\nextra = 1\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n"
        "[[path-overrides]]\npattern = 'bad/**suffix'\nallowed-modes = ['regular']\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n"
        "[[path-overrides]]\npattern = 'x'\nallowed-modes = []\n",
        "mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n"
        "[[path-overrides]]\npattern = 'x'\nallowed-modes = ['regular']\n"
        "[[path-overrides]]\npattern = 'x'\nallowed-modes = ['executable']\n",
    ],
)
def test_load_mode_policy_rejects_closed_schema_and_invalid_values(
    tmp_path: Path,
    document: str,
) -> None:
    with pytest.raises(ConfigurationError):
        load_mode_policy(_write(tmp_path / "mode.toml", document))


def test_load_mode_policy_rejects_entry_and_file_bounds(tmp_path: Path) -> None:
    entries = "\n".join(
        f"[[path-overrides]]\npattern = 'p{index}'\nallowed-modes = ['regular']"
        for index in range(MAX_MODE_POLICY_OVERRIDES + 1)
    )
    too_many = _write(
        tmp_path / "many.toml",
        f"mode-policy-version = 1\ndefault-allowed-modes = ['regular']\n{entries}\n",
    )
    oversized = tmp_path / "oversized.toml"
    oversized.write_bytes(b"#" * (MAX_MODE_POLICY_BYTES + 1))

    with pytest.raises(ConfigurationError, match="exceeds 128"):
        load_mode_policy(too_many)
    with pytest.raises(ConfigurationError, match="exceeds"):
        load_mode_policy(oversized)


def test_load_mode_policy_rejects_missing_invalid_and_non_utf8_files(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="cannot read"):
        load_mode_policy(tmp_path / "missing.toml")
    with pytest.raises(ConfigurationError, match="invalid mode policy TOML"):
        load_mode_policy(_write(tmp_path / "invalid.toml", "not = [valid"))
    invalid_utf8 = tmp_path / "invalid-utf8.toml"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="valid UTF-8"):
        load_mode_policy(invalid_utf8)
