use super::{
    config::LoadedConfig,
    policy::*,
    unicode::{fold, is_space},
};
use regex::Regex;
use std::{collections::HashSet, sync::LazyLock};

const MAX_ITEMS: usize = 128;
pub(super) const WARNING_CODES: &[&str] = &[
    "type.allowed",
    "type.case",
    "scope.required",
    "scope.forbidden",
    "scope.allowed",
    "scope.case",
    "header.length",
    "description.length",
    "description.case",
    "description.ending",
    "breaking.marker-pair",
    "body.required",
    "body.forbidden",
    "body.length",
    "body.word-count",
    "body.line-length",
    "body.paragraph-format",
    "footer.line-length",
    "footer.required-colon",
    "footer.required",
    "footer.forbidden",
    "footer.value",
    "reference.required",
    "typos.word",
];

fn text_ok(value: &str, max: usize) -> bool {
    static UNSAFE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]").unwrap());
    !value.is_empty() && value.chars().count() <= max && !UNSAFE.is_match(value)
}

fn strings<'a>(
    values: impl Iterator<Item = &'a str>,
    key: &str,
    max_len: usize,
    unique: bool,
    valid: impl Fn(&str) -> bool,
) -> Result<(), String> {
    let mut seen = HashSet::new();
    for (i, value) in values.enumerate() {
        if i >= MAX_ITEMS {
            return Err(format!("{key} exceeds {MAX_ITEMS} entries"));
        }
        if !text_ok(value, max_len) || !valid(value) {
            return Err(format!(
                "{key} contains an invalid, empty, oversized, or unsafe value"
            ));
        }
        if unique && !seen.insert(fold(value)) {
            return Err(format!("{key} contains a duplicate token"));
        }
    }
    Ok(())
}

fn type_token(s: &str) -> bool {
    !s.contains(['(', ')', ':', '!']) && !s.chars().any(is_space)
}
fn scope_token(s: &str) -> bool {
    !s.contains(['(', ')']) && !s.chars().any(is_space)
}
fn footer_token(s: &str) -> bool {
    s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        && !s.eq_ignore_ascii_case("BREAKING-CHANGE")
}

fn reachable(
    allowed: Option<&[String]>,
    default: Presence,
    overrides: &[(String, Presence)],
    key: &str,
) -> Result<Vec<Presence>, String> {
    strings(
        overrides.iter().map(|(k, _)| k.as_str()),
        key,
        128,
        true,
        type_token,
    )?;
    if let Some(types) = allowed {
        let types: HashSet<_> = types.iter().map(|s| fold(s)).collect();
        if overrides.iter().any(|(k, _)| !types.contains(&fold(k))) {
            return Err(format!("{key} contains a type outside allowed-types"));
        }
        Ok(types
            .iter()
            .map(|kind| {
                overrides
                    .iter()
                    .find(|(k, _)| fold(k) == *kind)
                    .map_or(default, |(_, p)| *p)
            })
            .collect())
    } else {
        Ok(std::iter::once(default)
            .chain(overrides.iter().map(|(_, v)| *v))
            .collect())
    }
}

pub(super) fn policy(config: &LoadedConfig, conventional_only: bool) -> Result<(), String> {
    let p = &config.policy;
    if p.message_format == MessageFormat::Plain && conventional_only {
        return Err("plain message-format does not support conventional-only settings".into());
    }
    if !(1..=10_000).contains(&config.max_commits) {
        return Err("max-commits must be an integer from 1 through 10000".into());
    }
    static LOGIN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9_.-]+(?:\[bot\])?\z").unwrap());
    strings(
        config.skip_pull_request_authors.iter().map(String::as_str),
        "skip-pull-request-authors",
        128,
        true,
        |s| LOGIN.is_match(s),
    )?;
    if let Some(types) = &p.allowed_types {
        if types.is_empty() {
            return Err("allowed-types must not be empty".into());
        }
        strings(
            types.iter().map(String::as_str),
            "allowed-types",
            128,
            true,
            type_token,
        )?;
    }
    let scopes = reachable(
        p.allowed_types.as_deref(),
        p.scope_policy,
        &p.scope_policy_by_type,
        "scope-policy-by-type",
    )?;
    if let Some(allowed) = &p.allowed_scopes {
        strings(
            allowed.iter().map(String::as_str),
            "allowed-scopes",
            128,
            true,
            scope_token,
        )?;
        if allowed.is_empty() && scopes.contains(&Presence::Required) {
            return Err(
                "allowed-scopes cannot be empty when a reachable scope policy is required".into(),
            );
        }
        if !allowed.is_empty() && scopes.iter().all(|p| *p == Presence::Forbidden) {
            return Err("allowed-scopes cannot contain values when every reachable scope policy is forbidden".into());
        }
    }
    let bodies = reachable(
        p.allowed_types.as_deref(),
        p.body_policy,
        &p.body_policy_by_type,
        "body-policy-by-type",
    )?;
    for (key, v, min, max) in [
        ("header-max-length", p.header_max_length, 1, 100_000),
        (
            "description-min-length",
            Some(p.description_min_length),
            1,
            10_000,
        ),
        (
            "description-max-length",
            p.description_max_length,
            1,
            100_000,
        ),
        ("body-max-length", p.body_max_length, 1, 100_000),
        ("body-min-length", Some(p.body_min_length), 0, 100_000),
        ("body-min-words", Some(p.body_min_words), 0, 100_000),
        ("body-max-line-length", p.body_max_line_length, 1, 100_000),
        (
            "footer-max-line-length",
            p.footer_max_line_length,
            1,
            100_000,
        ),
    ] {
        if v.is_some_and(|n| !(min..=max).contains(&n)) {
            return Err(format!("{key} must be an integer from {min} through {max}"));
        }
    }
    if p.description_max_length
        .is_some_and(|max| max < p.description_min_length)
    {
        return Err("description-max-length is smaller than description-min-length".into());
    }
    if p.body_max_length.is_some_and(|max| max < p.body_min_length) {
        return Err("body-max-length is smaller than body-min-length".into());
    }
    if p.body_max_length
        .is_some_and(|max| p.body_min_words > 0 && max < 2 * p.body_min_words - 1)
    {
        return Err("body-max-length cannot fit body-min-words".into());
    }
    if (p.body_min_length > 0 || p.body_min_words > 0)
        && bodies.iter().all(|p| *p == Presence::Forbidden)
    {
        return Err("body minimum must be zero when body-policy is forbidden".into());
    }
    let mut minimum_header = p.description_min_length;
    if p.message_format == MessageFormat::Conventional {
        minimum_header += 3;
        if scopes.iter().all(|p| *p == Presence::Required) {
            minimum_header += 3;
        }
    }
    if p.header_max_length.is_some_and(|max| max < minimum_header) {
        return Err(format!(
            "header-max-length cannot fit the configured minimum header length of {minimum_header}"
        ));
    }
    strings(
        p.ignored_headers.iter().map(String::as_str),
        "ignored-headers",
        256,
        false,
        |_| true,
    )?;
    validate_content(p)
}

fn validate_content(p: &CommitPolicy) -> Result<(), String> {
    let required = &p.required_footer_tokens;
    let colon = &p.required_colon_footer_tokens;
    let forbidden = &p.forbidden_footer_tokens;
    if required.len() + colon.len() + forbidden.len() > MAX_ITEMS {
        return Err("footer presence constraints exceed 128 combined entries".into());
    }
    for (key, values) in [
        ("required-footer-tokens", required),
        ("forbidden-footer-tokens", forbidden),
        ("required-colon-footer-tokens", colon),
    ] {
        strings(
            values.iter().map(String::as_str),
            key,
            128,
            true,
            footer_token,
        )?;
    }
    let forbidden: HashSet<_> = forbidden.iter().map(|s| s.to_ascii_lowercase()).collect();
    if required
        .iter()
        .chain(colon)
        .any(|s| forbidden.contains(&s.to_ascii_lowercase()))
    {
        return Err("required-footer-tokens and forbidden-footer-tokens overlap".into());
    }
    strings(
        p.footer_values.iter().map(|(k, _)| k.as_str()),
        "footer-values",
        128,
        true,
        footer_token,
    )?;
    let mut total = 0;
    for (token, values) in &p.footer_values {
        if forbidden.contains(&token.to_ascii_lowercase()) {
            return Err("footer-values must not constrain forbidden footer tokens".into());
        }
        total += values.len();
        if total > 256 {
            return Err("footer-values exceeds 256 combined values".into());
        }
        if values.is_empty() || values.iter().collect::<HashSet<_>>().len() != values.len() {
            return Err("footer-values requires nonempty unique exact values".into());
        }
        strings(
            values.iter().map(String::as_str),
            "footer-values",
            256,
            false,
            |s| s.trim_matches(is_space) == s,
        )?;
    }
    static ISSUE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\A[A-Za-z#][A-Za-z0-9_-]*\z").unwrap());
    strings(
        p.required_issue_prefixes.iter().map(String::as_str),
        "required-issue-prefixes",
        64,
        false,
        |s| ISSUE.is_match(s),
    )?;
    if p.required_issue_prefixes
        .iter()
        .collect::<HashSet<_>>()
        .len()
        != p.required_issue_prefixes.len()
    {
        return Err("required-issue-prefixes contains duplicate prefixes".into());
    }
    strings(
        p.warning_rules.iter().map(String::as_str),
        "warning-rules",
        64,
        true,
        |s| WARNING_CODES.contains(&s),
    )?;
    if p.message_format == MessageFormat::Plain
        && p.warning_rules.iter().any(|s| {
            ["type.", "scope.", "breaking."]
                .iter()
                .any(|prefix| s.starts_with(prefix))
        })
    {
        return Err("plain message-format cannot warn on conventional-only rules".into());
    }
    Ok(())
}
