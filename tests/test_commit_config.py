"""Tests for strict YAGA TOML discovery and commit policy parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from yaga.commits.checker import check_target
from yaga.commits.config import MAX_CONFIG_BYTES, load_config
from yaga.commits.models import (
    BreakingMarkerPolicy,
    CasePolicy,
    CommitPolicy,
    CommitTarget,
    DependabotPullRequestPolicy,
    EndingPolicy,
    MergePolicy,
    PresencePolicy,
    QualityInputMode,
    QualityProvider,
    QualityTask,
    TyposPolicy,
)
from yaga.errors import ConfigurationError


def write_pyproject(path: Path, commit: str = "") -> Path:
    project = path / "pyproject.toml"
    project.write_text(
        "[tool.yaga]\nconfig-version = 1\n\n[tool.yaga.commit]\n" + commit,
        encoding="utf-8",
    )
    return project


def test_missing_configuration_uses_spec_only_defaults(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    loaded = load_config(start=tmp_path)

    assert loaded.path is None
    assert loaded.policy.allowed_types is None
    assert loaded.policy.scope_policy is PresencePolicy.OPTIONAL
    assert loaded.policy.scope_policy_by_type == ()
    assert loaded.policy.body_min_words == 0
    assert loaded.policy.breaking_markers is BreakingMarkerPolicy.EITHER
    assert loaded.policy.required_footer_tokens == ()
    assert loaded.policy.forbidden_footer_tokens == ()
    assert loaded.policy.dependabot_pull_requests is DependabotPullRequestPolicy.CHECK
    assert loaded.policy.typos is TyposPolicy.SKIP
    assert loaded.policy.merge_commits is MergePolicy.IGNORE


def test_quality_policy_is_configurable_without_secrets(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        """
[tool.yaga.commit.quality]
provider = "bedrock"
task = "classification"
model = "amazon.nova-micro-v1:0"
threshold = 0.4
region = "us-west-2"
max-tokens = 12
max-input-tokens = 1024
input-mode = "title"
""",
    )

    loaded = load_config(project, start=tmp_path)

    assert loaded.policy.quality.provider is QualityProvider.BEDROCK
    assert loaded.policy.quality.task is QualityTask.CLASSIFICATION
    assert loaded.policy.quality.model_id == "amazon.nova-micro-v1:0"
    assert loaded.policy.quality.revision is None
    assert loaded.policy.quality.threshold == 0.4
    assert loaded.policy.quality.region == "us-west-2"
    assert loaded.policy.quality.max_tokens == 12
    assert loaded.policy.quality.max_input_tokens == 1024
    assert loaded.policy.quality.input_mode is QualityInputMode.TITLE


def test_quality_policy_accepts_zero_threshold(tmp_path: Path) -> None:
    project = write_pyproject(tmp_path, "[tool.yaga.commit.quality]\nthreshold = 0\n")

    loaded = load_config(project, start=tmp_path)

    assert loaded.policy.quality.threshold == 0.0


def test_quality_policy_selects_the_bedrock_default_model(tmp_path: Path) -> None:
    project = write_pyproject(tmp_path, '[tool.yaga.commit.quality]\nprovider = "bedrock"\n')

    settings = load_config(project, start=tmp_path).policy.quality

    assert settings.model_id == "amazon.nova-micro-v1:0"
    assert settings.revision is None


@pytest.mark.parametrize("revision", ["a", "deadbeef", "a" * 39, "a" * 41, "a" * 64, "A" * 40])
def test_quality_policy_rejects_non_full_model_revisions(tmp_path: Path, revision: str) -> None:
    project = write_pyproject(tmp_path, f'[tool.yaga.commit.quality]\nrevision = "{revision}"\n')

    with pytest.raises(ConfigurationError, match="40-character lowercase hexadecimal"):
        load_config(project, start=tmp_path)


@pytest.mark.parametrize(
    "quality",
    [
        '[tool.yaga.commit.quality]\nprovider = "unknown"\n',
        '[tool.yaga.commit.quality]\ntask = "unknown"\n',
        '[tool.yaga.commit.quality]\nprovider = "bedrock"\ntask = "seq2seq"\n',
        '[tool.yaga.commit.quality]\nprovider = "bedrock"\nrevision = "0123456789abcdef"\n',
        '[tool.yaga.commit.quality]\nmodel = "bad\nmodel"\n',
        '[tool.yaga.commit.quality]\nrevision = "main"\n',
        "[tool.yaga.commit.quality]\nmax-tokens = 0\n",
        "[tool.yaga.commit.quality]\nmax-input-tokens = 0\n",
        '[tool.yaga.commit.quality]\ninput-mode = "body"\n',
        "[tool.yaga.commit.quality]\nextra = true\n",
    ],
)
def test_quality_policy_rejects_invalid_values(tmp_path: Path, quality: str) -> None:
    project = write_pyproject(tmp_path, quality)
    with pytest.raises(ConfigurationError):
        load_config(project, start=tmp_path)


def test_commit_policy_preserves_the_legacy_positional_constructor() -> None:
    policy = CommitPolicy(
        1,
        ("feat",),
        CasePolicy.LOWER,
        PresencePolicy.REQUIRED,
        ("cli",),
        CasePolicy.UPPER,
        80,
        3,
        72,
        EndingPolicy.FORBID,
        PresencePolicy.OPTIONAL,
        10,
        100,
        MergePolicy.REJECT,
        ("Revert *",),
        64,
    )

    assert policy.body_min_length == 10
    assert policy.body_max_line_length == 100
    assert policy.merge_commits is MergePolicy.REJECT
    assert policy.ignored_headers == ("Revert *",)
    assert policy.max_commits == 64
    assert policy.body_min_words == 0
    assert policy.breaking_markers is BreakingMarkerPolicy.EITHER
    assert policy.required_footer_tokens == ()
    assert policy.forbidden_footer_tokens == ()
    assert policy.scope_policy_by_type == ()
    assert policy.dependabot_pull_requests is DependabotPullRequestPolicy.CHECK


def test_nearest_pyproject_is_discovered_from_a_nested_directory(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["feat", "fix"]\ntype-case = "lower"\nmax-commits = 12\n',
    )
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)

    loaded = load_config(start=nested)

    assert loaded.path == project
    assert loaded.policy.allowed_types == ("feat", "fix")
    assert loaded.policy.type_case is CasePolicy.LOWER
    assert loaded.policy.max_commits == 12


@pytest.mark.parametrize(
    "value, expected", [("skip", TyposPolicy.SKIP), ("check", TyposPolicy.CHECK)]
)
def test_typos_policy_accepts_its_closed_values(
    tmp_path: Path, value: str, expected: TyposPolicy
) -> None:
    project = write_pyproject(tmp_path, f'typos = "{value}"\n')

    loaded = load_config(project)

    assert loaded.policy.typos is expected


def test_typos_policy_rejects_values_outside_its_closed_enum(tmp_path: Path) -> None:
    project = write_pyproject(tmp_path, 'typos = "auto"\n')

    with pytest.raises(ConfigurationError, match="typos must be one of"):
        load_config(project)


def test_standalone_configuration_wins_in_the_same_directory(tmp_path: Path) -> None:
    write_pyproject(tmp_path, 'allowed-types = ["feat"]\n')
    standalone = tmp_path / ".yaga.toml"
    standalone.write_text(
        'config-version = 1\n[commit]\nallowed-types = ["docs"]\n',
        encoding="utf-8",
    )

    loaded = load_config(start=tmp_path)

    assert loaded.path == standalone
    assert loaded.policy.allowed_types == ("docs",)


def test_explicit_configuration_does_not_merge_discovered_values(tmp_path: Path) -> None:
    write_pyproject(tmp_path, 'allowed-types = ["feat"]\n')
    explicit = tmp_path / "policy.toml"
    explicit.write_text(
        'config-version = 1\n[commit]\nscope-policy = "required"\n',
        encoding="utf-8",
    )

    loaded = load_config(explicit, start=tmp_path)

    assert loaded.path == explicit.resolve()
    assert loaded.policy.allowed_types is None
    assert loaded.policy.scope_policy is PresencePolicy.REQUIRED


def test_discovery_does_not_escape_the_git_root(tmp_path: Path) -> None:
    write_pyproject(tmp_path, 'allowed-types = ["feat"]\n')
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    nested = repository / "nested"
    nested.mkdir()

    assert load_config(start=nested).path is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[tool.yaga]\nunknown = true\n", "unknown YAGA"),
        ("[tool.yaga]\nconfig-version = 2\n", "config-version"),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nmerge-commits = "sometimes"\n',
            "merge-commits",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nallowed-types = ["feat", "FEAT"]\n',
            "duplicate token",
        ),
        (
            "[tool.yaga]\n[tool.yaga.commit]\nallowed-types = []\n",
            "must not be empty",
        ),
        (
            "[tool.yaga]\n[tool.yaga.commit]\n"
            'scope-policy = "forbidden"\nallowed-scopes = ["cli"]\n',
            "allowed-scopes",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nscope-policy = "required"\nallowed-scopes = []\n',
            "cannot be empty",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nallowed-scopes = ["   "]\n',
            "invalid token",
        ),
        (
            '[tool.yaga]\n[tool.yaga.commit]\nignored-headers = ["\\u009b31m*"]\n',
            "unsafe value",
        ),
    ],
)
def test_invalid_configuration_fails_loudly(tmp_path: Path, content: str, message: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_config(project)


def test_explicit_empty_scope_list_forbids_any_named_scope(tmp_path: Path) -> None:
    project = write_pyproject(tmp_path, "allowed-scopes = []\n")

    loaded = load_config(project)

    assert loaded.policy.allowed_scopes == ()


def test_scope_policy_by_type_preserves_configured_spelling_and_order(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["feat", "revert"]\n'
        'scope-policy-by-type = { FEAT = "required", Revert = "forbidden" }\n',
    )

    policy = load_config(project).policy

    assert policy.scope_policy_by_type == (
        ("FEAT", PresencePolicy.REQUIRED),
        ("Revert", PresencePolicy.FORBIDDEN),
    )


@pytest.mark.parametrize(
    ("configured", "message"),
    [
        ("scope-policy-by-type = []\n", "must be a table"),
        ('scope-policy-by-type = { feat = "sometimes" }\n', "must be one of"),
        ("scope-policy-by-type = { feat = true }\n", "must be a string"),
        ('scope-policy-by-type = { "" = "required" }\n', "invalid type token"),
        ('scope-policy-by-type = { "feat(scope)" = "required" }\n', "invalid type token"),
        (
            f'scope-policy-by-type = {{ "{"x" * 129}" = "required" }}\n',
            "invalid type token",
        ),
        (
            'scope-policy-by-type = { "fi\u200bx" = "required" }\n',
            "invalid type token",
        ),
        (
            'scope-policy-by-type = { feat = "required", FEAT = "optional" }\n',
            "duplicate type token",
        ),
        (
            'scope-policy-by-type = { "ß" = "required", ss = "optional" }\n',
            "duplicate type token",
        ),
    ],
)
def test_scope_policy_by_type_rejects_invalid_mappings(
    tmp_path: Path,
    configured: str,
    message: str,
) -> None:
    project = write_pyproject(tmp_path, configured)

    with pytest.raises(ConfigurationError, match=message):
        load_config(project)


@pytest.mark.parametrize("count", [128, 129])
def test_scope_policy_by_type_bounds_entries(tmp_path: Path, count: int) -> None:
    entries = ", ".join(f'Type{index} = "optional"' for index in range(count))
    project = write_pyproject(tmp_path, f"scope-policy-by-type = {{ {entries} }}\n")

    if count == 128:
        assert len(load_config(project).policy.scope_policy_by_type) == 128
    else:
        with pytest.raises(ConfigurationError, match="exceeds 128 entries"):
            load_config(project)


def test_scope_policy_by_type_must_name_an_allowed_type(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["feat"]\nscope-policy-by-type = { fix = "required" }\n',
    )

    with pytest.raises(ConfigurationError, match="type 'fix' is not in allowed-types"):
        load_config(project)


def test_empty_allowed_scopes_rejects_any_reachable_required_override(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'allowed-scopes = []\nscope-policy-by-type = { feat = "required" }\n',
    )

    with pytest.raises(ConfigurationError, match="reachable scope policy is required"):
        load_config(project)


def test_allowed_types_limit_scope_policy_reachability(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["feat"]\n'
        'scope-policy = "required"\n'
        'scope-policy-by-type = { feat = "forbidden" }\n'
        "allowed-scopes = []\n",
    )

    assert load_config(project).policy.allowed_scopes == ()


def test_nonempty_allowed_scopes_accepts_a_reachable_optional_override(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'scope-policy = "forbidden"\n'
        'scope-policy-by-type = { feat = "optional" }\n'
        'allowed-scopes = ["cli"]\n',
    )

    assert load_config(project).policy.allowed_scopes == ("cli",)


def test_nonempty_allowed_scopes_rejects_only_forbidden_reachable_policies(
    tmp_path: Path,
) -> None:
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["feat"]\n'
        'scope-policy = "optional"\n'
        'scope-policy-by-type = { feat = "forbidden" }\n'
        'allowed-scopes = ["cli"]\n',
    )

    with pytest.raises(ConfigurationError, match="every reachable scope policy is forbidden"):
        load_config(project)


def test_header_minimum_uses_effective_scope_policy_candidates(tmp_path: Path) -> None:
    forbidden_override = write_pyproject(
        tmp_path,
        'allowed-types = ["feature"]\n'
        'scope-policy = "required"\n'
        'scope-policy-by-type = { feature = "forbidden" }\n'
        "header-max-length = 10\n",
    )

    assert load_config(forbidden_override).policy.header_max_length == 10

    required_override = write_pyproject(
        tmp_path,
        'allowed-types = ["f"]\nscope-policy-by-type = { f = "required" }\nheader-max-length = 4\n',
    )
    with pytest.raises(ConfigurationError, match="minimum header length of 7"):
        load_config(required_override)


def test_header_minimum_includes_explicit_overrides_without_allowed_types(
    tmp_path: Path,
) -> None:
    project = write_pyproject(
        tmp_path,
        'scope-policy = "required"\n'
        'scope-policy-by-type = { x = "forbidden" }\n'
        "header-max-length = 4\n",
    )

    assert load_config(project).policy.header_max_length == 4


def test_header_minimum_is_a_conservative_structural_bound(tmp_path: Path) -> None:
    configured_spelling = write_pyproject(
        tmp_path,
        'allowed-types = ["feature"]\nheader-max-length = 8\n',
    )

    assert load_config(configured_spelling).policy.header_max_length == 8

    casefold_equivalent = write_pyproject(
        tmp_path,
        'allowed-types = ["ss"]\n'
        'scope-policy = "required"\n'
        'allowed-scopes = ["x"]\n'
        "header-max-length = 7\n",
    )
    policy = load_config(casefold_equivalent).policy

    assert check_target(CommitTarget(label="message", message="ß(x): a"), policy).valid


def test_small_but_satisfiable_length_limits_are_supported(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'allowed-types = ["f"]\nheader-max-length = 4\nbody-max-line-length = 1\n',
    )

    loaded = load_config(project)

    assert loaded.policy.header_max_length == 4
    assert loaded.policy.body_max_line_length == 1


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("either", BreakingMarkerPolicy.EITHER),
        ("paired", BreakingMarkerPolicy.PAIRED),
    ],
)
def test_breaking_markers_accepts_its_closed_values(
    tmp_path: Path,
    configured: str,
    expected: BreakingMarkerPolicy,
) -> None:
    project = write_pyproject(tmp_path, f'breaking-markers = "{configured}"\n')

    assert load_config(project).policy.breaking_markers is expected


@pytest.mark.parametrize("value", ['"both"', '"PAIRed"', "true", "1"])
def test_breaking_markers_rejects_values_outside_its_closed_enum(
    tmp_path: Path,
    value: str,
) -> None:
    project = write_pyproject(tmp_path, f"breaking-markers = {value}\n")

    with pytest.raises(ConfigurationError, match="breaking-markers must be"):
        load_config(project)


@pytest.mark.parametrize("minimum", [0, 100_000])
def test_body_min_words_accepts_its_closed_integer_range(tmp_path: Path, minimum: int) -> None:
    project = write_pyproject(tmp_path, f"body-min-words = {minimum}\n")

    loaded = load_config(project)

    assert loaded.policy.body_min_words == minimum


@pytest.mark.parametrize("value", ["-1", "100001", "true", '"2"', "2.0"])
def test_body_min_words_rejects_invalid_values(tmp_path: Path, value: str) -> None:
    project = write_pyproject(tmp_path, f"body-min-words = {value}\n")

    with pytest.raises(ConfigurationError, match="body-min-words must be an integer"):
        load_config(project)


@pytest.mark.parametrize("value", ["skip", "check"])
def test_body_paragraph_splitting_accepts_its_closed_values(
    tmp_path: Path,
    value: str,
) -> None:
    project = write_pyproject(tmp_path, f'body-paragraph-splitting = "{value}"\n')

    assert load_config(project).policy.body_paragraph_splitting.value == value


@pytest.mark.parametrize("value", ["0", "true", '"warn"', "1.0"])
def test_body_paragraph_splitting_rejects_invalid_values(
    tmp_path: Path,
    value: str,
) -> None:
    project = write_pyproject(tmp_path, f"body-paragraph-splitting = {value}\n")

    with pytest.raises(
        ConfigurationError,
        match="body-paragraph-splitting must be (?:a string in|one of)",
    ):
        load_config(project)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("check", DependabotPullRequestPolicy.CHECK),
        ("skip", DependabotPullRequestPolicy.SKIP),
    ],
)
def test_dependabot_pull_requests_accepts_its_closed_values(
    tmp_path: Path,
    configured: str,
    expected: DependabotPullRequestPolicy,
) -> None:
    project = write_pyproject(tmp_path, f'dependabot-pull-requests = "{configured}"\n')

    assert load_config(project).policy.dependabot_pull_requests is expected


@pytest.mark.parametrize("value", ['"ignore"', '"SKIP"', "true", "1"])
def test_dependabot_pull_requests_rejects_values_outside_its_closed_enum(
    tmp_path: Path,
    value: str,
) -> None:
    project = write_pyproject(tmp_path, f"dependabot-pull-requests = {value}\n")

    with pytest.raises(ConfigurationError, match="dependabot-pull-requests must be"):
        load_config(project)


def test_footer_token_policies_preserve_configured_spelling_and_order(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'required-footer-tokens = ["Signed-off-by", "Refs"]\n'
        'forbidden-footer-tokens = ["WIP", "Do-Not-Merge"]\n',
    )

    policy = load_config(project).policy

    assert policy.required_footer_tokens == ("Signed-off-by", "Refs")
    assert policy.forbidden_footer_tokens == ("WIP", "Do-Not-Merge")


def test_footer_token_policies_accept_empty_lists_and_closed_boundaries(tmp_path: Path) -> None:
    longest = "A" * 128
    project = write_pyproject(
        tmp_path,
        f'required-footer-tokens = ["A", "{longest}"]\nforbidden-footer-tokens = []\n',
    )

    policy = load_config(project).policy

    assert policy.required_footer_tokens == ("A", longest)
    assert policy.forbidden_footer_tokens == ()


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("required-footer-tokens", '"Signed-off-by"', "array of strings"),
        ("required-footer-tokens", '["Refs", 1]', "array of strings"),
        ("required-footer-tokens", '["has_underscore"]', "invalid token"),
        ("required-footer-tokens", '["Réfs"]', "invalid token"),
        ("required-footer-tokens", '["Refs:"]', "invalid token"),
        ("required-footer-tokens", '["A", "a"]', "duplicate token"),
        ("forbidden-footer-tokens", '["WIP", "wip"]', "duplicate token"),
        ("required-footer-tokens", f'["{"A" * 129}"]', "oversized"),
    ],
)
def test_footer_token_policies_reject_invalid_values(
    tmp_path: Path,
    key: str,
    value: str,
    message: str,
) -> None:
    project = write_pyproject(tmp_path, f"{key} = {value}\n")

    with pytest.raises(ConfigurationError, match=message):
        load_config(project)


@pytest.mark.parametrize(
    "token",
    ["BREAKING CHANGE", "breaking change", "BREAKING-CHANGE", "breaking-change"],
)
@pytest.mark.parametrize("key", ["required-footer-tokens", "forbidden-footer-tokens"])
def test_footer_token_policies_reserve_breaking_change_markers(
    tmp_path: Path,
    key: str,
    token: str,
) -> None:
    project = write_pyproject(tmp_path, f'{key} = ["{token}"]\n')

    with pytest.raises(ConfigurationError, match="reserved breaking-change token"):
        load_config(project)


def test_footer_token_policies_reject_case_insensitive_overlap(tmp_path: Path) -> None:
    project = write_pyproject(
        tmp_path,
        'required-footer-tokens = ["Signed-off-by"]\nforbidden-footer-tokens = ["SIGNED-OFF-BY"]\n',
    )

    with pytest.raises(ConfigurationError, match="overlap at token 'SIGNED-OFF-BY'"):
        load_config(project)


@pytest.mark.parametrize("total", [128, 129])
def test_footer_token_policies_bound_the_combined_entry_count(
    tmp_path: Path,
    total: int,
) -> None:
    required = ", ".join(f'"R{index}"' for index in range(64))
    forbidden = ", ".join(f'"F{index}"' for index in range(total - 64))
    project = write_pyproject(
        tmp_path,
        f"required-footer-tokens = [{required}]\nforbidden-footer-tokens = [{forbidden}]\n",
    )

    if total == 128:
        policy = load_config(project).policy
        assert len(policy.required_footer_tokens) + len(policy.forbidden_footer_tokens) == 128
    else:
        with pytest.raises(ConfigurationError, match="exceed 128 combined entries"):
            load_config(project)


@pytest.mark.parametrize(
    ("minimums", "incompatible"),
    [
        ("body-min-length = 1\n", "body-min-length"),
        ("body-min-words = 1\n", "body-min-words"),
        (
            "body-min-length = 1\nbody-min-words = 1\n",
            "body-min-length and body-min-words",
        ),
    ],
)
def test_forbidden_body_requires_zero_body_minima(
    tmp_path: Path,
    minimums: str,
    incompatible: str,
) -> None:
    project = write_pyproject(
        tmp_path,
        f'body-policy = "forbidden"\n{minimums}',
    )

    with pytest.raises(
        ConfigurationError,
        match=rf"{incompatible} must be zero when body-policy is forbidden",
    ):
        load_config(project)


def test_invalid_utf8_and_oversized_files_are_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.toml"
    invalid.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="valid UTF-8"):
        load_config(invalid)

    oversized = tmp_path / "oversized.toml"
    oversized.write_bytes(b"x" * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ConfigurationError, match="exceeds"):
        load_config(oversized)
