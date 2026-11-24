use regex::Regex;
use serde::Serialize;
use std::sync::LazyLock;

use super::policy::*;
use super::unicode::{CommitUnicode, is_space};
use super::{
    MAX_MESSAGE_BYTES, ParsedCommit, content, glob,
    parser::{normalized_text, parse_normalized},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Severity {
    Error,
    Warning,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct Diagnostic {
    pub code: &'static str,
    pub message: String,
    pub line: usize,
    pub column: usize,
    pub severity: Severity,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CheckResult {
    pub header: String,
    pub skipped_reason: Option<&'static str>,
    pub diagnostics: Vec<Diagnostic>,
}

impl CheckResult {
    pub fn valid(&self) -> bool {
        self.diagnostics
            .iter()
            .all(|d| d.severity != Severity::Error)
    }
    pub(super) fn finding(&mut self, code: &'static str, message: impl Into<String>, line: usize) {
        self.diagnostics.push(Diagnostic {
            code,
            message: message.into(),
            line,
            column: 1,
            severity: Severity::Error,
        });
    }
}

/// Evaluate one selected UTF-8 message. The caller supplies merge identity and
/// chooses the complete-message or title-only policy boundary.
pub fn check_message(
    message: &str,
    policy: &CommitPolicy,
    is_merge: bool,
    header_only: bool,
) -> CheckResult {
    check_message_with_unicode(
        message,
        policy,
        is_merge,
        header_only,
        &CommitUnicode::default(),
    )
}

/// Evaluate the same policy with the embedding runtime's character properties.
pub fn check_message_with_unicode(
    message: &str,
    policy: &CommitPolicy,
    is_merge: bool,
    header_only: bool,
    unicode: &CommitUnicode,
) -> CheckResult {
    let normalized = normalized_text(message);
    let header = normalized.split('\n').next().unwrap_or_default();
    let mut result = CheckResult {
        header: header.into(),
        skipped_reason: None,
        diagnostics: Vec::new(),
    };
    if normalized.len() > MAX_MESSAGE_BYTES {
        result.finding(
            "message.size",
            format!("message exceeds the hard {MAX_MESSAGE_BYTES}-byte limit"),
            1,
        );
        return result;
    }
    if is_merge {
        match policy.merge_commits {
            MergePolicy::Ignore => {
                result.skipped_reason = Some("merge commit");
                return result;
            }
            MergePolicy::Reject => {
                result.finding(
                    "merge.rejected",
                    "merge commits are rejected by the configured policy",
                    1,
                );
                return result;
            }
            MergePolicy::Check => (),
        }
    }
    if policy
        .ignored_headers
        .iter()
        .any(|pattern| glob::matches(pattern, header))
    {
        result.skipped_reason = Some("ignored header");
        return result;
    }
    let needs_body_lines = !header_only
        && (policy.body_max_line_length.is_some()
            || policy.body_paragraph_splitting == ParagraphSplitting::Check);
    let Some(parsed) = parse_normalized(
        &normalized,
        policy.message_format,
        policy.footer_syntax,
        needs_body_lines,
        unicode,
    ) else {
        let message = match policy.message_format {
            MessageFormat::Plain => {
                "expected one nonempty plain title without outer whitespace or unsafe controls"
            }
            MessageFormat::Conventional => "expected <type>[optional scope][!]: <description>",
        };
        result.finding("syntax.header", message, 1);
        return result;
    };
    if !parsed.separator_valid {
        result.finding(
            "syntax.separator",
            "body or footers must be separated from the header by a blank line",
            2,
        );
    }
    if let Some(kind) = parsed.commit_type {
        check_component(
            &mut result,
            kind,
            policy.type_case,
            policy.allowed_types.as_deref(),
            false,
            unicode,
        );
        let (presence, _) = presence_for_type(
            policy.scope_policy,
            &policy.scope_policy_by_type,
            kind,
            unicode,
        );
        if let Some(scope) = parsed.scope {
            if presence == Presence::Forbidden {
                result.finding("scope.forbidden", "scopes are forbidden by policy", 1);
            }
            check_component(
                &mut result,
                scope,
                policy.scope_case,
                policy.allowed_scopes.as_deref(),
                true,
                unicode,
            );
        } else if presence == Presence::Required {
            result.finding("scope.required", "a scope is required by policy", 1);
        }
    }
    let header_length = measure(parsed.header, policy.length_unit);
    if let Some(max) = policy.header_max_length.filter(|&max| header_length > max) {
        result.finding(
            "header.length",
            format!("header has {header_length} characters; maximum is {max}"),
            1,
        );
    }
    check_description(&mut result, parsed.description, policy, unicode);
    if !header_only {
        check_body(&mut result, &parsed, policy, unicode);
        content::check_footers_and_references(&mut result, &parsed, policy);
    }
    for diagnostic in &mut result.diagnostics {
        if super::validate::WARNING_CODES.contains(&diagnostic.code)
            && policy
                .warning_rules
                .iter()
                .any(|code| code == diagnostic.code)
        {
            diagnostic.severity = Severity::Warning;
        }
    }
    result
}

pub(super) fn resolve_presence<'a>(
    default: Presence,
    overrides: &'a [(String, Presence)],
    kind: &str,
) -> (Presence, Option<&'a str>) {
    presence_for_type(default, overrides, kind, &CommitUnicode::default())
}

fn presence_for_type<'a>(
    default: Presence,
    overrides: &'a [(String, Presence)],
    kind: &str,
    unicode: &CommitUnicode,
) -> (Presence, Option<&'a str>) {
    let folded = unicode.fold(kind);
    overrides
        .iter()
        .find(|(key, _)| unicode.fold(key) == folded)
        .map_or((default, None), |(key, value)| (*value, Some(key.as_str())))
}

fn check_component(
    result: &mut CheckResult,
    value: &str,
    case: Case,
    allowed: Option<&[String]>,
    scope: bool,
    unicode: &CommitUnicode,
) {
    let (name, case_code, allowed_code, key) = if scope {
        ("scope", "scope.case", "scope.allowed", "allowed-scopes")
    } else {
        ("type", "type.case", "type.allowed", "allowed-types")
    };
    let expected = match case {
        Case::Lower if !unicode.case_unchanged(value, true) => Some("lowercase"),
        Case::Upper if !unicode.case_unchanged(value, false) => Some("uppercase"),
        _ => None,
    };
    if let Some(expected) = expected {
        result.finding(
            case_code,
            format!("{name} {} must be {expected}", unicode.repr(value)),
            1,
        );
    }
    if let Some(allowed) = allowed {
        let candidate = unicode.fold(value);
        if !allowed.iter().any(|token| unicode.fold(token) == candidate) {
            result.finding(
                allowed_code,
                format!("{name} {} is not in {key}", unicode.repr(value)),
                1,
            );
        }
    }
}

fn measure(s: &str, unit: LengthUnit) -> usize {
    match unit {
        LengthUnit::Utf16 => s.encode_utf16().count(),
        LengthUnit::Codepoints => s.chars().count(),
    }
}

fn check_description(
    result: &mut CheckResult,
    desc: &str,
    policy: &CommitPolicy,
    unicode: &CommitUnicode,
) {
    static UPPER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\p{Lu}").unwrap());
    let length = measure(desc, policy.length_unit);
    let min = policy.description_min_length;
    if length < min {
        result.finding(
            "description.length",
            format!("description has {length} characters; minimum is {min}"),
            1,
        );
    }
    if let Some(max) = policy.description_max_length.filter(|&max| length > max) {
        result.finding(
            "description.length",
            format!("description has {length} characters; maximum is {max}"),
            1,
        );
    }
    if policy.description_case == DescriptionCase::ForbidInitialUppercase
        && desc
            .chars()
            .next()
            .and_then(|c| unicode.0.get(&c))
            .map_or_else(|| UPPER.is_match(desc), |p| p.category == "Lu")
    {
        result.finding(
            "description.case",
            "description must not start with an uppercase letter",
            1,
        );
    }
    let punct = desc.ends_with(['.', '!', '?']);
    let ending_message = match policy.description_ending {
        Ending::ForbidPeriod if desc.ends_with('.') => Some("description must not end with '.'"),
        Ending::Forbid if punct => Some("description must not end with '.', '!', or '?'"),
        Ending::Require if !punct => Some("description must end with '.', '!', or '?'"),
        _ => None,
    };
    if let Some(message) = ending_message {
        result.finding("description.ending", message, 1);
    }
}

fn check_body(
    result: &mut CheckResult,
    parsed: &ParsedCommit<&str>,
    policy: &CommitPolicy,
    unicode: &CommitUnicode,
) {
    static ALNUM: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{L}\p{N}]").unwrap());
    if policy.breaking_markers == BreakingMarkers::Paired
        && parsed.breaking_header != parsed.breaking_footer
    {
        result.finding(
            "breaking.marker-pair",
            "breaking changes must use both ! and a BREAKING CHANGE footer",
            1,
        );
    }
    let (presence, _) = presence_for_type(
        policy.body_policy,
        &policy.body_policy_by_type,
        parsed.commit_type.unwrap_or(""),
        unicode,
    );
    let start = parsed.body_start_line;
    let body = parsed.body.trim_matches(is_space);
    if body.is_empty() {
        if presence == Presence::Required {
            result.finding("body.required", "a body is required by policy", 1);
        }
    } else {
        if presence == Presence::Forbidden {
            result.finding("body.forbidden", "a body is forbidden by policy", start);
        }
        let min = policy.body_min_length;
        if min > 0 || policy.body_max_length.is_some() {
            let length = measure(body, policy.length_unit);
            if presence != Presence::Forbidden && length < min {
                result.finding(
                    "body.length",
                    format!("body has {length} characters; minimum is {min}"),
                    start,
                );
            }
            if let Some(max) = policy.body_max_length.filter(|&max| length > max) {
                result.finding(
                    "body.length",
                    format!("body has {length} characters; maximum is {max}"),
                    start,
                );
            }
        }
        let min_words = policy.body_min_words;
        if presence != Presence::Forbidden && min_words > 0 {
            let count = body
                .split(is_space)
                .filter(|s| {
                    unicode.contains(s, &ALNUM, |_, category| category.starts_with(['L', 'N']))
                })
                .take(min_words)
                .count();
            if count < min_words {
                let noun = if count == 1 { "word" } else { "words" };
                result.finding(
                    "body.word-count",
                    format!("body has {count} {noun}; minimum is {min_words}"),
                    start,
                );
            }
        }
    }
    check_line_lengths(result, &parsed.body_lines, start, policy, false);
    if policy.body_paragraph_splitting == ParagraphSplitting::Check
        && let Some(line) = sentence_split(&parsed.body_lines, unicode)
    {
        result.finding(
            "body.paragraph-format",
            "blank line likely splits one sentence; keep the sentence in one paragraph",
            start + line,
        );
    }
    check_line_lengths(
        result,
        &parsed.footer_lines,
        parsed.footer_start_line.unwrap_or(1),
        policy,
        true,
    );
}

fn check_line_lengths(
    result: &mut CheckResult,
    lines: &[&str],
    start: usize,
    policy: &CommitPolicy,
    footer: bool,
) {
    static URL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?:^|[^A-Za-z0-9_])https?://[^\s\x1c-\x1f]+").unwrap());
    let (part, code, max) = if footer {
        (
            "footer",
            "footer.line-length",
            policy.footer_max_line_length,
        )
    } else {
        ("body", "body.line-length", policy.body_max_line_length)
    };
    let Some(max) = max else {
        return;
    };
    for (i, line) in lines.iter().enumerate() {
        let length = measure(line, policy.length_unit);
        if length > max
            && !(policy.line_length_urls == LineLengthUrls::Exempt && URL.is_match(line))
        {
            result.finding(
                code,
                format!("{part} line has {length} characters; maximum is {max}"),
                start + i,
            );
            break;
        }
    }
}

fn list_item(text: &str, unicode: &CommitUnicode) -> bool {
    static LIST_ITEM: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^(?:[-*+]|\d+[.)])[\s\x1c-\x1f]").unwrap());
    static DIGIT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d").unwrap());
    if unicode.0.is_empty() || text.is_ascii() || text.starts_with(['-', '*', '+']) {
        return LIST_ITEM.is_match(text);
    }
    // Only the decimal digits in a numbered list can change with a Unicode release.
    let tail = text.trim_start_matches(|c: char| {
        unicode.0.get(&c).map_or_else(
            || DIGIT.is_match(c.encode_utf8(&mut [0; 4])),
            |p| p.category == "Nd",
        )
    });
    tail.len() < text.len()
        && tail
            .strip_prefix(['.', ')'])
            .is_some_and(|s| s.starts_with(is_space))
}

fn sentence_split(lines: &[&str], unicode: &CommitUnicode) -> Option<usize> {
    let mut start = 0;
    let mut previous: Option<&str> = None;
    for end in 0..=lines.len() {
        if end < lines.len() && !lines[end].is_empty() {
            continue;
        }
        if end == start + 1 {
            let text = lines[start].trim_start_matches(is_space);
            if !text.is_empty() && !list_item(text, unicode) {
                if previous.is_some_and(|s| {
                    s.ends_with([',', ';', ':', '—', '–', '-'])
                        || text.starts_with(|c: char| {
                            unicode
                                .0
                                .get(&c)
                                .map_or_else(|| c.is_lowercase(), |p| p.lowercase)
                        })
                }) {
                    return Some(start);
                }
                previous = Some(text.trim_end_matches(is_space));
                start = end + 1;
                continue;
            }
        }
        start = end + 1;
        previous = None;
    }
    None
}
