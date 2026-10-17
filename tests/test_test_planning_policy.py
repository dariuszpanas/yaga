"""Closed version-one policy, resource boundaries, and exact source identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast

import pytest

from yaga.errors import ConfigurationError
from yaga.test_planning.limits import (
    MAX_CELLS,
    MAX_DEPENDENCIES,
    MAX_DISTINCT_CAPABILITIES,
    MAX_DISTINCT_TAGS,
    MAX_PATTERNS,
    MAX_POLICY_BYTES,
)
from yaga.test_planning.models import (
    LoadedTestPolicy,
    Profile,
    QualificationPolicy,
    SelectionMode,
    SelectionRule,
    Suite,
    UnknownPathPolicy,
)
from yaga.test_planning.policy import load_test_policy, parse_test_policy

EXAMPLES = Path(__file__).resolve().parents[1] / "docs/designs/examples/test-planning"
MINIMAL = b"""test-policy-version = 1
[[suites]]
id = "unit"
binding = "pytest-unit"
[profiles.development]
mode = "selective"
"""


def minimal_policy(**changes: Any) -> QualificationPolicy:
    return replace(
        QualificationPolicy(
            1, (Suite("unit", "pytest-unit"),), (Profile("dev", SelectionMode.SELECTIVE),)
        ),
        **changes,
    )


def policy_bytes(policy: QualificationPolicy) -> bytes:
    """Serialize valid models in the tests; the runtime has no policy writer."""
    lines = [f"test-policy-version = {policy.test_policy_version}"]
    lines.append(f"unknown-paths = {json.dumps(policy.unknown_paths.value)}")
    lines.append(f"full-on = {json.dumps(policy.full_on)}")
    lines.append(f"no-impact = {json.dumps(policy.no_impact)}")
    for suite in policy.suites:
        lines.append("[[suites]]")
        for key in ("id", "binding", "tags", "variants", "needs", "requires"):
            lines.append(f"{key} = {json.dumps(getattr(suite, key))}")
    for profile in policy.profiles:
        lines.extend(
            [
                f"[profiles.{json.dumps(profile.name)}]",
                f"mode = {json.dumps(profile.mode.value)}",
                f"always = {json.dumps(profile.always)}",
            ]
        )
    for rule in policy.rules:
        lines.extend(
            [
                "[[rules]]",
                f"id = {json.dumps(rule.id)}",
                f"when-any = {json.dumps(rule.when_any)}",
                f"select = {json.dumps(rule.select)}",
                f"select-tags = {json.dumps(rule.select_tags)}",
            ]
        )
    return ("\n".join(lines) + "\n").encode()


def test_defaults_are_immutable_and_file_hash_covers_exact_bytes(tmp_path: Path) -> None:
    source = tmp_path / "explicit.toml"
    source.write_bytes(MINIMAL)
    loaded = load_test_policy(source)

    assert loaded.path == source.resolve()
    assert loaded.sha256 == hashlib.sha256(MINIMAL).hexdigest()
    assert loaded.policy == QualificationPolicy(
        1, (Suite("unit", "pytest-unit"),), (Profile("development", SelectionMode.SELECTIVE),)
    )
    with pytest.raises(FrozenInstanceError):
        cast(Any, loaded.policy).rules = ()
    commented = b"# a comment\n" + MINIMAL
    source.write_bytes(commented)
    again = load_test_policy(source)
    assert again.policy == loaded.policy
    assert again.sha256 != loaded.sha256
    assert again.sha256 == hashlib.sha256(commented).hexdigest()


@pytest.mark.parametrize(("name", "count", "cells"), [("polyglot", 10, 11), ("django-ray", 5, 8)])
def test_complete_design_examples_load_and_round_trip(name: str, count: int, cells: int) -> None:
    path = EXAMPLES / f"{name}.toml"
    loaded = load_test_policy(path)
    assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(loaded.policy.suites) == count
    assert sum(len(suite.variants) for suite in loaded.policy.suites) == cells
    assert parse_test_policy(policy_bytes(loaded.policy)) == loaded.policy
    assert {profile.name for profile in loaded.policy.profiles} == {
        "development",
        "integration",
        "release",
    }


@pytest.mark.parametrize(
    ("original", "replacement", "message"),
    [
        (b"test-policy-version = 1", b"", "missing.*test-policy-version"),
        (b"test-policy-version = 1", b"test-policy-version = true", "integer 1"),
        (b"test-policy-version = 1", b"test-policy-version = 2", "integer 1"),
        (b"test-policy-version = 1", b'test-policy-version = "1"', "integer 1"),
        (b"test-policy-version = 1", b"test-policy-version = 1.0", "integer 1"),
        (b'id = "unit"', b"", "missing.*id"),
        (b'binding = "pytest-unit"', b"", "missing.*binding"),
        (b'mode = "selective"', b"", "missing.*mode"),
        (b'mode = "selective"', b'mode = "skip"', "selective or full"),
        (b'mode = "selective"', b"mode = true", "must be a string"),
        (b'id = "unit"', b"id = 123", "must be a string"),
        (b'binding = "pytest-unit"', b"binding = []", "must be a string"),
        (b"[profiles.development]", b"[profiles.Development]", "lowercase ASCII token"),
    ],
)
def test_required_fields_and_scalar_types_are_strict(
    original: bytes, replacement: bytes, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_test_policy(MINIMAL.replace(original, replacement))


@pytest.mark.parametrize(
    "body",
    [
        b"test-policy-version = 1\nsuites = []\n[profiles.dev]\nmode = 'full'",
        b"test-policy-version = 1\nsuites = {}\n[profiles.dev]\nmode = 'full'",
        b"test-policy-version = 1\nsuites = ['unit']\n[profiles.dev]\nmode = 'full'",
        b"test-policy-version = 1\nprofiles = {}\n[[suites]]\nid='unit'\nbinding='unit'",
        b"test-policy-version = 1\nprofiles = []\n[[suites]]\nid='unit'\nbinding='unit'",
        b"test-policy-version = 1\nprofiles = {dev = 1}\n[[suites]]\nid='unit'\nbinding='unit'",
        b"test-policy-version = 1\n[profiles.dev]\nmode = 'full'",
        b"test-policy-version = 1\n[[suites]]\nid='unit'\nbinding='unit'",
    ],
)
def test_catalog_and_profiles_are_required_nonempty_typed_tables(body: bytes) -> None:
    with pytest.raises(ConfigurationError):
        parse_test_policy(body)


@pytest.mark.parametrize(
    "key", ["include", "command", "environment", "expression", "repo", "format"]
)
@pytest.mark.parametrize("location", ["root", "suite", "profile", "rule"])
def test_every_table_has_a_closed_key_set(key: str, location: str) -> None:
    extra = f'{key} = "unsupported"\n'.encode()
    if location == "root":
        body = extra + MINIMAL
    elif location == "suite":
        body = MINIMAL.replace(b"[profiles.development]", extra + b"[profiles.development]")
    elif location == "profile":
        body = MINIMAL + extra
    else:
        body = MINIMAL + b"[[rules]]\nid='rule'\nwhen-any=['src/**']\nselect=['unit']\n" + extra
    with pytest.raises(ConfigurationError, match=f"unknown .* key '{key}'"):
        parse_test_policy(body)


@pytest.mark.parametrize("value", ["false", "1", '"unit"', "{}", "[1]", '["unit", true]'])
@pytest.mark.parametrize(
    ("location", "key"),
    [
        ("root", "full-on"),
        ("root", "no-impact"),
        ("suite", "tags"),
        ("suite", "variants"),
        ("suite", "needs"),
        ("suite", "requires"),
        ("profile", "always"),
        ("rule", "when-any"),
        ("rule", "select"),
        ("rule", "select-tags"),
    ],
)
def test_list_fields_reject_every_non_string_array_shape(
    location: str, key: str, value: str
) -> None:
    field = f"{key} = {value}\n".encode()
    if location == "root":
        body = field + MINIMAL
    elif location == "suite":
        body = MINIMAL.replace(b"[profiles.development]", field + b"[profiles.development]")
    elif location == "profile":
        body = MINIMAL + field
    else:
        fields = {"id": "'rule'", "when-any": "['src/**']", "select": "['unit']"}
        fields[key] = value
        body = (
            MINIMAL + b"[[rules]]\n" + "".join(f"{k} = {v}\n" for k, v in fields.items()).encode()
        )
    with pytest.raises(ConfigurationError, match="must be (an array of strings|a string)"):
        parse_test_policy(body)


@pytest.mark.parametrize("value", ['"skip"', "true", "1", "[]", "{}"])
def test_unknown_path_policy_is_closed(value: str) -> None:
    with pytest.raises(ConfigurationError, match="unknown-paths"):
        parse_test_policy(f"unknown-paths = {value}\n".encode() + MINIMAL)
    assert (
        parse_test_policy(b'unknown-paths="error"\n' + MINIMAL).unknown_paths
        is UnknownPathPolicy.ERROR
    )


@pytest.mark.parametrize(
    "bad", ["", "A", "1a", "a_b", ".a", "a.", "a--b", "a..b", "a-.b", "a/b", "é", "a\n", "a" * 65]
)
def test_token_grammar_rejects_unsafe_or_noncanonical_names(bad: str) -> None:
    for field in ("id", "binding"):
        with pytest.raises(ValueError, match="lowercase ASCII token"):
            replace(Suite("unit", "binding"), **{field: bad})
    for field in ("tags", "variants", "needs", "requires"):
        with pytest.raises(ValueError, match="lowercase ASCII token"):
            replace(Suite("unit", "binding"), **{field: (bad,)})
    with pytest.raises(ValueError):
        Profile(bad, SelectionMode.FULL)
    with pytest.raises(ValueError):
        SelectionRule(bad, ("src/**",), ("unit",))


def test_token_limit_is_inclusive_and_no_case_normalization_occurs() -> None:
    longest = "a" * 64
    policy = minimal_policy(
        suites=(Suite(longest, longest, (longest,), (longest,), requires=(longest,)),)
    )
    assert parse_test_policy(policy_bytes(policy)) == policy
    assert Suite("alpha0.beta-2", "binding").id == "alpha0.beta-2"
    dotted = replace(policy, profiles=(Profile("development.fast", SelectionMode.FULL),))
    assert parse_test_policy(policy_bytes(dotted)) == dotted


@pytest.mark.parametrize("key", ["tags", "variants", "needs", "requires"])
def test_suite_lists_are_unique_and_capped_at_sixteen(key: str) -> None:
    valid = tuple(f"item-{i}" for i in range(16))
    assert len(getattr(replace(Suite("suite", "binding"), **{key: valid}), key)) == 16
    with pytest.raises(ValueError, match="16"):
        replace(Suite("suite", "binding"), **{key: (*valid, "overflow")})
    with pytest.raises(ValueError, match="unique"):
        replace(Suite("suite", "binding"), **{key: ("one", "one")})
    with pytest.raises(ValueError):
        replace(Suite("suite", "binding"), **{key: ["one"]})


def test_empty_variants_and_empty_rule_selection_are_invalid() -> None:
    with pytest.raises(ValueError, match="variants"):
        Suite("unit", "binding", variants=())
    with pytest.raises(ValueError, match="nonempty combined"):
        SelectionRule("rule", ("src/**",))
    with pytest.raises(ValueError, match="when-any"):
        SelectionRule("rule", (), ("unit",))
    with pytest.raises(ConfigurationError, match="nonempty combined"):
        parse_test_policy(MINIMAL + b"[[rules]]\nid='rule'\nwhen-any=['src/**']\n")


@pytest.mark.parametrize("key", ["full_on", "no_impact"])
def test_policy_pattern_lists_are_unique_and_capped_at_128(key: str) -> None:
    valid = tuple(f"src/{i}/**" for i in range(128))
    policy = minimal_policy(**{key: valid})
    assert parse_test_policy(policy_bytes(policy)) == policy
    with pytest.raises(ValueError, match="128"):
        minimal_policy(**{key: (*valid, "extra")})
    with pytest.raises(ValueError, match="unique"):
        minimal_policy(**{key: ("src/**", "src/**")})


@pytest.mark.parametrize(
    "pattern",
    ["", "../x", "/x", "a//b", "a/**b", "a\\b", "a\n", "a" * 513],
)
def test_patterns_use_the_shared_anchored_grammar(pattern: str) -> None:
    with pytest.raises(ValueError):
        SelectionRule("rule", (pattern,), ("unit",))
    with pytest.raises(ValueError):
        minimal_policy(full_on=(pattern,))
    with pytest.raises(ValueError):
        minimal_policy(no_impact=(pattern,))


def test_other_glob_and_expression_characters_are_preserved_as_literals() -> None:
    patterns = ("a?", "a[bc]", "a{b,c}", "${NAME}", "é/**")
    policy = minimal_policy(full_on=patterns, no_impact=patterns)
    assert parse_test_policy(policy_bytes(policy)) == policy


def test_pattern_list_and_global_distinct_limits_allow_cross_rule_reuse() -> None:
    patterns = tuple(f"src/{i}" for i in range(MAX_PATTERNS))
    rules = tuple(
        SelectionRule(f"rule-{i}", patterns[i * 128 : (i + 1) * 128], ("unit",)) for i in range(8)
    )
    policy = minimal_policy(rules=rules, full_on=patterns[:128], no_impact=patterns[:128])
    assert parse_test_policy(policy_bytes(policy)) == policy
    with pytest.raises(ValueError, match="1024 distinct"):
        replace(policy, rules=(*rules, SelectionRule("extra", ("extra",), ("unit",))))
    with pytest.raises(ValueError, match="128"):
        SelectionRule("rule", patterns[:129], ("unit",))
    with pytest.raises(ValueError, match="unique"):
        SelectionRule("rule", ("src", "src"), ("unit",))


def test_catalog_profile_and_rule_maximum_counts_are_inclusive() -> None:
    suites = tuple(Suite(f"suite-{i}", "binding") for i in range(128))
    profiles = tuple(Profile(f"profile-{i}", SelectionMode.FULL) for i in range(16))
    rules = tuple(SelectionRule(f"rule-{i}", ("src/**",), ("suite-0",)) for i in range(128))
    policy = QualificationPolicy(1, suites, profiles, rules)
    assert parse_test_policy(policy_bytes(policy)) == policy
    for field, extra, match in [
        ("suites", Suite("extra", "binding"), "128"),
        ("profiles", Profile("extra", SelectionMode.FULL), "16"),
        ("rules", SelectionRule("extra", ("src",), ("suite-0",)), "128"),
    ]:
        with pytest.raises(ValueError, match=match):
            replace(policy, **{field: (*getattr(policy, field), extra)})
    for field in ("suites", "profiles", "rules"):
        with pytest.raises(ValueError, match="unique"):
            replace(policy, **{field: (getattr(policy, field)[0],) * 2})


def test_selection_arrays_have_bounded_unique_ids_and_tags() -> None:
    ids = tuple(f"suite-{i}" for i in range(128))
    suites = tuple(Suite(item, "binding", tags=(item,)) for item in ids)
    rule = SelectionRule("rule", ("src",), ids, ids)
    profile = Profile("full", SelectionMode.FULL, ids)
    policy = QualificationPolicy(1, suites, (profile,), (rule,))
    assert parse_test_policy(policy_bytes(policy)) == policy
    for field in ("select", "select_tags"):
        with pytest.raises(ValueError, match="128"):
            replace(rule, **{field: (*ids, "extra")})
        with pytest.raises(ValueError, match="unique"):
            replace(rule, **{field: ("suite-0", "suite-0")})
    with pytest.raises(ValueError, match="128"):
        replace(profile, always=(*ids, "extra"))
    with pytest.raises(ValueError, match="unique"):
        replace(profile, always=("suite-0", "suite-0"))


def test_expanded_cells_are_bounded_without_silent_reduction() -> None:
    variants = tuple(f"v{i}" for i in range(16))
    suites = tuple(Suite(f"suite-{i}", "binding", variants=variants) for i in range(32))
    policy = minimal_policy(suites=suites)
    assert sum(len(suite.variants) for suite in policy.suites) == MAX_CELLS
    assert parse_test_policy(policy_bytes(policy)) == policy
    with pytest.raises(ValueError, match="512 expanded"):
        replace(policy, suites=(*suites, Suite("extra", "binding")))


@pytest.mark.parametrize(
    ("field", "maximum", "message"),
    [("tags", MAX_DISTINCT_TAGS, "tags"), ("requires", MAX_DISTINCT_CAPABILITIES, "capabilities")],
)
def test_distinct_metadata_limits_are_global_and_count_reuse_once(
    field: str, maximum: int, message: str
) -> None:
    metadata = tuple(f"item-{i}" for i in range(maximum))
    suites = tuple(
        replace(Suite(f"suite-{i}", "binding"), **{field: metadata[i * 16 : (i + 1) * 16]})
        for i in range(8)
    )
    policy = minimal_policy(
        suites=(*suites, replace(Suite("reuse", "binding"), **{field: metadata[:16]}))
    )
    assert parse_test_policy(policy_bytes(policy)) == policy
    with pytest.raises(ValueError, match=f"128 distinct {message}"):
        replace(
            policy,
            suites=(*policy.suites, replace(Suite("extra", "binding"), **{field: ("extra",)})),
        )


def test_dependency_edge_bound_is_inclusive() -> None:
    roots = tuple(Suite(f"root-{i}", "binding") for i in range(32))
    leaves = tuple(
        Suite(f"leaf-{i}", "binding", needs=tuple(s.id for s in roots[:16])) for i in range(32)
    )
    policy = minimal_policy(suites=(*roots, *leaves))
    assert sum(len(suite.needs) for suite in policy.suites) == MAX_DEPENDENCIES
    assert parse_test_policy(policy_bytes(policy)) == policy
    with pytest.raises(ValueError, match="512 prerequisite edges"):
        replace(policy, suites=(*roots[:-1], replace(roots[-1], needs=("root-0",)), *leaves))


def test_every_reference_must_exist_even_when_full_profile_would_ignore_rules() -> None:
    base = minimal_policy(profiles=(Profile("full", SelectionMode.FULL),))
    cases = [
        {"suites": (Suite("unit", "binding", needs=("missing",)),)},
        {"profiles": (Profile("full", SelectionMode.FULL, ("missing",)),)},
        {"rules": (SelectionRule("rule", ("src",), ("missing",)),)},
        {"rules": (SelectionRule("rule", ("src",), ("unit",), ("missing",)),)},
    ]
    for changes in cases:
        with pytest.raises(ValueError, match="unknown"):
            replace(base, **changes)
    with pytest.raises(ValueError, match="own id"):
        Suite("unit", "binding", needs=("unit",))
    with pytest.raises(ValueError, match="prerequisite cycle"):
        replace(
            base,
            suites=(
                Suite("a", "b", needs=("b",)),
                Suite("b", "b", needs=("c",)),
                Suite("c", "b", needs=("a",)),
            ),
        )


@pytest.mark.parametrize("field", ["suites", "profiles", "rules", "full_on", "no_impact"])
def test_mutable_model_containers_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        minimal_policy(**{field: []})


def test_wrong_direct_model_types_fail_with_value_errors() -> None:
    bad = cast(Any, "wrong")
    for kwargs in (
        {"test_policy_version": True},
        {"suites": (bad,)},
        {"profiles": (bad,)},
        {"rules": (bad,)},
        {"unknown_paths": "full"},
    ):
        with pytest.raises(ValueError):
            minimal_policy(**kwargs)
    with pytest.raises(ValueError):
        Profile("dev", cast(Any, "selective"))
    with pytest.raises(ValueError):
        Profile("dev", SelectionMode.FULL, cast(Any, []))
    for field in ("select", "select_tags", "when_any"):
        with pytest.raises(ValueError):
            replace(SelectionRule("rule", ("src",), ("unit",)), **{field: []})


def test_loaded_model_identity_is_strict(tmp_path: Path) -> None:
    valid = LoadedTestPolicy(minimal_policy(), tmp_path, "a" * 64)
    for kwargs in (
        {"policy": None},
        {"path": Path("relative")},
        {"path": str(tmp_path)},
        {"sha256": "A" * 64},
        {"sha256": "f" * 63},
        {"sha256": None},
    ):
        with pytest.raises(ValueError):
            replace(valid, **kwargs)


def test_exact_byte_size_and_utf8_boundaries(tmp_path: Path) -> None:
    padded = MINIMAL + b"#" + b"x" * (MAX_POLICY_BYTES - len(MINIMAL) - 1)
    assert len(padded) == MAX_POLICY_BYTES
    assert parse_test_policy(padded) == parse_test_policy(MINIMAL)
    assert parse_test_policy(b"\xef\xbb\xbf" + MINIMAL) == parse_test_policy(MINIMAL)
    path = tmp_path / "max.toml"
    path.write_bytes(padded)
    assert load_test_policy(path).sha256 == hashlib.sha256(padded).hexdigest()
    for raw in (padded + b"x", b"\xff", b"\x00", b"["):
        with pytest.raises(ConfigurationError):
            parse_test_policy(raw)
        path.write_bytes(raw)
        with pytest.raises(ConfigurationError):
            load_test_policy(path)
    with pytest.raises(ConfigurationError, match="must be bytes"):
        parse_test_policy(cast(Any, bytearray(MINIMAL)))


def test_errors_are_bounded_and_sanitized() -> None:
    key = "\u001b[31m" + "x" * 4000 + "\u202e"
    raw = f"{json.dumps(key)} = true\n".encode() + MINIMAL
    with pytest.raises(ConfigurationError) as caught:
        parse_test_policy(raw)
    assert len(str(caught.value)) < 600
    assert "\u001b" not in str(caught.value)
    assert "\u202e" not in str(caught.value)
    # This is valid TOML. Some runtimes parse it and reject its schema; others
    # reach a parser recursion limit. Both paths must yield a bounded error.
    with pytest.raises(ConfigurationError) as nested:
        parse_test_policy(("[" + ".".join(["a"] * 2000) + "]").encode())
    assert len(str(nested.value)) < 600
    with pytest.raises(ConfigurationError):
        parse_test_policy(b"test-policy-version = " + b"1" * 10_000)


@pytest.mark.parametrize("error_type", [ValueError, RecursionError])
def test_parser_errors_do_not_expose_untrusted_details(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    def fail(_text: str) -> dict[str, object]:
        raise error_type("\u001b[31m" + "untrusted" * 1000)

    monkeypatch.setattr("yaga.test_planning.policy.tomllib.loads", fail)
    with pytest.raises(ConfigurationError) as caught:
        parse_test_policy(MINIMAL)
    assert str(caught.value) == "invalid test policy TOML"


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_loader_requires_an_explicit_readable_regular_file(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "policy"
    if kind == "directory":
        path.mkdir()
    with pytest.raises(ConfigurationError, match="cannot read regular test policy"):
        load_test_policy(path)


def test_no_discovery_includes_or_environment_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".yaga.toml").write_bytes(MINIMAL)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigurationError):
        load_test_policy(tmp_path / "missing.toml")
    with pytest.raises(ConfigurationError):
        parse_test_policy(b'include=".yaga.toml"\n' + MINIMAL)
    with pytest.raises(ConfigurationError):
        parse_test_policy(MINIMAL.replace(b'"pytest-unit"', b'"${RUNNER}"'))
