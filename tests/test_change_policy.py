"""Tests for strict explicit changed-path policy loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from yaga.changes.models import (
    MAX_CHANGE_PATTERNS,
    MAX_CHANGE_RULE_NAME_CHARS,
    MAX_CHANGE_RULES,
    ChangePolicy,
    ChangeRule,
    LoadedChangePolicy,
)
from yaga.changes.patterns import MAX_CHANGE_PATTERN_BYTES
from yaga.changes.policy import MAX_CHANGE_POLICY_BYTES, load_change_policy
from yaga.errors import ConfigurationError


def write_policy(directory: Path, body: str | bytes) -> Path:
    path = directory / "change-policy.toml"
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    return path


def policy_document(*rule_lines: str) -> str:
    return "\n".join(("change-policy-version = 1", *rule_lines, ""))


def rule_document(
    *,
    name: str = "python-source-needs-tests",
    when_any: str = '["src/**/*.py"]',
    require_any: str = '["tests/**/*.py"]',
) -> tuple[str, ...]:
    return (
        "[[rules]]",
        f"name = {json.dumps(name)}",
        f"when-any = {when_any}",
        f"require-any = {require_any}",
    )


def test_minimal_policy_loads_one_explicit_rule_and_resolved_path(tmp_path: Path) -> None:
    path = write_policy(tmp_path, policy_document(*rule_document()))

    loaded = load_change_policy(path)

    assert loaded == LoadedChangePolicy(
        policy=ChangePolicy(
            change_policy_version=1,
            rules=(
                ChangeRule(
                    name="python-source-needs-tests",
                    when_any=("src/**/*.py",),
                    require_any=("tests/**/*.py",),
                ),
            ),
        ),
        path=path.resolve(),
    )


def test_rule_and_pattern_order_is_preserved_and_literals_are_not_expanded(
    tmp_path: Path,
) -> None:
    path = write_policy(
        tmp_path,
        policy_document(
            *rule_document(
                name="python-source-needs-tests",
                when_any='["src/**/*.py", "$SRC/%literal%/*.py"]',
                require_any='["tests/**/*.py", "pyproject.toml"]',
            ),
            *rule_document(
                name="metadata-needs-lock",
                when_any='["pyproject.toml"]',
                require_any='["uv.lock"]',
            ),
        ),
    )

    policy = load_change_policy(path).policy

    assert tuple(rule.name for rule in policy.rules) == (
        "python-source-needs-tests",
        "metadata-needs-lock",
    )
    assert policy.rules[0].when_any == ("src/**/*.py", "$SRC/%literal%/*.py")
    assert policy.rules[0].require_any == ("tests/**/*.py", "pyproject.toml")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "[[rules]]\nname='rule'\nwhen-any=['a']\nrequire-any=['b']\n",
            "missing change-policy-version",
        ),
        ("change-policy-version = 1\n", "missing rules"),
        ("change-policy-version = 1\nrules = []\n", "rules must not be empty"),
        ("change-policy-version = 2\nrules=[]\n", "integer 1"),
        ("change-policy-version = true\nrules=[]\n", "integer 1"),
        ('change-policy-version = "1"\nrules=[]\n', "integer 1"),
        ("change-policy-version = 1\n[rules]\n", "array of tables"),
        ("change-policy-version = 1\nrules = [1]\n", "array of tables"),
    ],
)
def test_required_top_level_contract_is_strict(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        load_change_policy(write_policy(tmp_path, body))


@pytest.mark.parametrize(
    "key",
    ["include", "environment", "expression", "command", "repo", "range", "format"],
)
def test_unknown_or_runtime_top_level_keys_are_rejected(tmp_path: Path, key: str) -> None:
    body = policy_document(f'{key} = "unsupported"', *rule_document())

    with pytest.raises(ConfigurationError, match=rf"unknown change policy key.*{key}"):
        load_change_policy(write_policy(tmp_path, body))


@pytest.mark.parametrize("key", ["name", "when-any", "require-any"])
def test_every_rule_key_is_required(tmp_path: Path, key: str) -> None:
    lines = list(rule_document())
    prefix = f"{key} ="
    lines = [line for line in lines if not line.startswith(prefix)]

    with pytest.raises(ConfigurationError, match=rf"missing required key.*{key}"):
        load_change_policy(write_policy(tmp_path, policy_document(*lines)))


def test_unknown_rule_keys_are_rejected(tmp_path: Path) -> None:
    body = policy_document(*rule_document(), 'unless-any = ["docs/**"]')

    with pytest.raises(ConfigurationError, match=r"unknown rules\[1\] key.*unless-any"):
        load_change_policy(write_policy(tmp_path, body))


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Uppercase",
        "under_score",
        "-leading",
        "trailing-",
        "double--hyphen",
        "café",
        "a" * (MAX_CHANGE_RULE_NAME_CHARS + 1),
    ],
)
def test_rule_names_are_bounded_lowercase_ascii_slugs(tmp_path: Path, name: str) -> None:
    body = policy_document(*rule_document(name=name))

    with pytest.raises(ConfigurationError, match="lowercase ASCII slug"):
        load_change_policy(write_policy(tmp_path, body))


def test_rule_name_must_be_a_string_and_names_must_be_unique(tmp_path: Path) -> None:
    non_string = policy_document(
        "[[rules]]",
        "name = true",
        'when-any = ["src/**"]',
        'require-any = ["tests/**"]',
    )
    with pytest.raises(ConfigurationError, match=r"rules\[1\]\.name must be a string"):
        load_change_policy(write_policy(tmp_path, non_string))

    duplicate = policy_document(*rule_document(), *rule_document())
    with pytest.raises(ConfigurationError, match="duplicate name"):
        load_change_policy(write_policy(tmp_path, duplicate))


@pytest.mark.parametrize("key", ["when-any", "require-any"])
def test_rule_pattern_arrays_are_strict_nonempty_bounded_and_unique(
    tmp_path: Path,
    key: str,
) -> None:
    field = "when_any" if key == "when-any" else "require_any"
    for value, message in (
        ('"src/**"', "array of strings"),
        ("[true]", "array of strings"),
        ("[]", "must not be empty"),
        (
            "[" + ",".join(f'"path-{index}"' for index in range(MAX_CHANGE_PATTERNS + 1)) + "]",
            "exceeds",
        ),
        ('["src/**", "src/**"]', "must be unique"),
    ):
        arguments = {field: value}
        body = policy_document(*rule_document(**arguments))
        with pytest.raises(ConfigurationError, match=rf"{key}.*{message}"):
            load_change_policy(write_policy(tmp_path, body))


def test_invalid_patterns_are_configuration_errors_without_interpolation(tmp_path: Path) -> None:
    for pattern in ("/absolute/**", "src/**.py", "src/../tests/**", "é" * 257):
        body = policy_document(*rule_document(when_any=f"[{json.dumps(pattern)}]"))
        with pytest.raises(ConfigurationError, match=r"invalid rules\[1\].*change pattern"):
            load_change_policy(write_policy(tmp_path, body))

    assert len(("é" * 257).encode()) > MAX_CHANGE_PATTERN_BYTES


def test_policy_rule_count_is_bounded(tmp_path: Path) -> None:
    body = policy_document(
        *(
            line
            for index in range(MAX_CHANGE_RULES + 1)
            for line in rule_document(name=f"r-{index}")
        )
    )

    with pytest.raises(ConfigurationError, match=rf"rules exceeds {MAX_CHANGE_RULES} entries"):
        load_change_policy(write_policy(tmp_path, body))


def test_policy_accepts_exact_byte_limit_and_rejects_one_more(tmp_path: Path) -> None:
    prefix = policy_document(*rule_document()).encode() + b"#"
    exact = prefix + b"x" * (MAX_CHANGE_POLICY_BYTES - len(prefix))
    path = write_policy(tmp_path, exact)

    assert load_change_policy(path).policy.rules[0].name == "python-source-needs-tests"

    path.write_bytes(exact + b"x")
    with pytest.raises(ConfigurationError, match=rf"exceeds {MAX_CHANGE_POLICY_BYTES} bytes"):
        load_change_policy(path)


def test_invalid_utf8_invalid_toml_and_missing_files_are_configuration_errors(
    tmp_path: Path,
) -> None:
    path = write_policy(tmp_path, b"\xff")
    with pytest.raises(ConfigurationError, match="not valid UTF-8"):
        load_change_policy(path)

    path.write_text("change-policy-version = [", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="invalid change policy TOML"):
        load_change_policy(path)

    with pytest.raises(ConfigurationError, match="cannot read change policy"):
        load_change_policy(tmp_path / "missing.toml")


def test_utf8_bom_is_accepted(tmp_path: Path) -> None:
    raw = b"\xef\xbb\xbf" + policy_document(*rule_document()).encode()

    assert load_change_policy(write_policy(tmp_path, raw)).policy.change_policy_version == 1


def test_programmatic_policy_models_preserve_the_same_invariants(tmp_path: Path) -> None:
    rule = ChangeRule("rule-1", ("src/**",), ("tests/**",))

    with pytest.raises(ValueError, match="version"):
        ChangePolicy(True, (rule,))
    with pytest.raises(ValueError, match="version"):
        ChangePolicy(cast(int, 1.0), (rule,))
    with pytest.raises(ValueError, match="1 through"):
        ChangePolicy(1, ())
    with pytest.raises(ValueError, match="unique"):
        ChangePolicy(1, (rule, rule))
    with pytest.raises(ValueError, match="lowercase ASCII slug"):
        ChangeRule("RULE", ("src/**",), ("tests/**",))
    with pytest.raises(ValueError, match="patterns must be unique"):
        ChangeRule("rule", ("src/**", "src/**"), ("tests/**",))
    with pytest.raises(ValueError, match="path must be absolute"):
        LoadedChangePolicy(ChangePolicy(1, (rule,)), Path("relative.toml"))

    absolute = tmp_path.resolve() / "not-required-to-exist.toml"
    assert LoadedChangePolicy(ChangePolicy(1, (rule,)), absolute).path == absolute
