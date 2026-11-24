use std::collections::{HashMap, HashSet};

use super::{
    CheckResult, CommitPolicy, ParsedCommit, footer_start,
    unicode::{is_space, repr},
};

/// Search for a configured ASCII prefix followed by a standalone 1–20 digit
/// positive ID. Trying possible numeric suffixes handles overlapping prefixes,
/// including prefixes that end in digits, in at most 20 lookups per token.
pub fn has_issue_reference(message: &str, prefixes: &[String]) -> bool {
    let prefixes: HashSet<_> = prefixes.iter().map(String::as_str).collect();
    for token in message.split(is_space) {
        let token = token.trim_matches(|ch| "()[]{}<>,.;:!?\"'".contains(ch));
        if token.len() > 84 || !token.is_ascii() {
            continue;
        }
        for (offset, b) in token.bytes().rev().take(20).enumerate() {
            if !b.is_ascii_digit() {
                break;
            }
            if b != b'0' && prefixes.contains(&token[..token.len() - offset - 1]) {
                return true;
            }
        }
    }
    false
}

pub(super) fn check_footers_and_references(
    result: &mut CheckResult,
    parsed: &ParsedCommit<&str>,
    policy: &CommitPolicy,
) {
    let mut missing_colon: HashSet<_> = policy
        .required_colon_footer_tokens
        .iter()
        .map(String::as_str)
        .collect();
    if !missing_colon.is_empty() {
        for line in &parsed.footer_lines {
            if let Some((key, value)) = line.split_once(':') {
                let trimmed = value.trim_start_matches(is_space);
                if !trimmed.is_empty() && trimmed.len() < value.len() {
                    missing_colon.remove(key);
                }
            }
        }
        for token in &policy.required_colon_footer_tokens {
            if missing_colon.contains(token.as_str()) {
                result.finding(
                    "footer.required-colon",
                    format!(
                        "a nonempty case-sensitive {} colon footer is required",
                        repr(token)
                    ),
                    1,
                );
            }
        }
    }

    let start = parsed.footer_start_line.unwrap_or(1);
    let footers = || {
        parsed
            .footer_lines
            .iter()
            .enumerate()
            .filter_map(|(i, line)| footer_start(line, start + i, policy.footer_syntax))
    };
    if !policy.required_footer_tokens.is_empty() || !policy.forbidden_footer_tokens.is_empty() {
        let mut missing: HashSet<_> = policy
            .required_footer_tokens
            .iter()
            .map(|s| s.to_ascii_lowercase())
            .collect();
        let forbidden: HashMap<_, _> = policy
            .forbidden_footer_tokens
            .iter()
            .map(|s| (s.to_ascii_lowercase(), s))
            .collect();
        let mut first_forbidden = None;
        for footer in footers() {
            let key = footer.token.to_ascii_lowercase();
            missing.remove(&key);
            if first_forbidden.is_none() {
                first_forbidden = forbidden.get(&key).map(|token| (footer.line, *token));
            }
        }
        if let Some(token) = policy
            .required_footer_tokens
            .iter()
            .find(|s| missing.contains(&s.to_ascii_lowercase()))
        {
            let remaining = missing.len() - 1;
            let mut message = format!("required footer token {} is missing", repr(token));
            if remaining > 0 {
                let noun = if remaining == 1 { "token" } else { "tokens" };
                message.push_str(&format!(
                    "; {remaining} additional required footer {noun} missing"
                ));
            }
            result.finding("footer.required", message, 1);
        }
        if let Some((line, token)) = first_forbidden {
            result.finding(
                "footer.forbidden",
                format!("footer token {} is forbidden by policy", repr(token)),
                line,
            );
        }
    }
    if !policy.required_issue_prefixes.is_empty()
        && !has_issue_reference(parsed.message, &policy.required_issue_prefixes)
    {
        result.finding(
            "reference.required",
            "a configured work-item prefix followed by a positive ASCII ID is required",
            1,
        );
    }
    if policy.footer_values.is_empty() {
        return;
    }
    let mut active: HashMap<_, _> = policy
        .footer_values
        .iter()
        .map(|(s, values)| (s.to_ascii_lowercase(), values))
        .collect();
    let mut starts = footers().peekable();
    while let Some(footer) = starts.next() {
        let key = footer.token.to_ascii_lowercase();
        let Some(allowed) = active.get(&key) else {
            continue;
        };
        let end = starts
            .peek()
            .map_or(parsed.footer_lines.len(), |next| next.line - start);
        let offset = footer.line - start;
        let skip = footer.token.len() + if footer.separator == ':' { 1 } else { 2 };
        let mut text = String::from(&parsed.footer_lines[offset][skip..]);
        for line in &parsed.footer_lines[offset + 1..end] {
            text.push('\n');
            text.push_str(line);
        }
        if !allowed.iter().any(|s| s == text.trim_matches(is_space)) {
            result.finding(
                "footer.value",
                format!(
                    "footer {} must use one of its configured exact values",
                    repr(footer.token)
                ),
                footer.line,
            );
            active.remove(&key);
        }
    }
}
