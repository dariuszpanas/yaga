//! Bounded display of untrusted text for compatibility with YAGA reports.

use regex::Regex;
use std::fmt::Write;
use std::sync::LazyLock;

static CONTROLS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Cf}]").unwrap());
static NON_PRINTABLE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{C}\p{Z}]").unwrap());

pub(crate) fn bound(value: &str, maximum: usize) -> String {
    if value.chars().nth(maximum).is_none() {
        return value.to_owned();
    }
    if maximum == 0 {
        return String::new();
    }
    value.chars().take(maximum - 1).chain(['…']).collect()
}

/// Preserve ordinary whitespace in JSON fields while neutralizing terminal controls.
pub fn json_text(value: &str, maximum: usize) -> String {
    bound(&CONTROLS.replace_all(value, "?"), maximum)
}

/// Collapse whitespace in a one-line terminal label after neutralizing controls.
pub fn safe_text(value: &str, maximum: usize) -> String {
    let cleaned = CONTROLS.replace_all(value, "?");
    bound(
        &cleaned.split_whitespace().collect::<Vec<_>>().join(" "),
        maximum,
    )
}

/// Quote the same way as Python's string repr, which appears in the public text reports.
pub fn python_repr(value: &str) -> String {
    python_repr_with(value, |_| None)
}

pub(crate) fn python_repr_with(
    value: &str,
    non_printable: impl Fn(char) -> Option<bool>,
) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(value.len() + 2);
    out.push(quote);
    let mut bytes = [0; 4];
    for c in value.chars() {
        match c {
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote || c == '\\' => {
                out.push('\\');
                out.push(c);
            }
            c if c != ' '
                && non_printable(c)
                    .unwrap_or_else(|| NON_PRINTABLE.is_match(c.encode_utf8(&mut bytes))) =>
            {
                let cp = c as u32;
                if cp < 256 {
                    write!(out, "\\x{cp:02x}").unwrap();
                } else if cp <= 65535 {
                    write!(out, "\\u{cp:04x}").unwrap();
                } else {
                    write!(out, "\\U{cp:08x}").unwrap();
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// Escape workflow command data and truncate at whole percent escapes.
pub fn github_data(value: &str, maximum: usize) -> String {
    github_command_field(value, maximum, false)
}

/// Escape a GitHub workflow command property, including its extra separators.
pub fn github_property(value: &str, maximum: usize) -> String {
    github_command_field(value, maximum, true)
}

fn github_command_field(value: &str, maximum: usize, property: bool) -> String {
    let mut escaped = value
        .replace('%', "%25")
        .replace('\r', "%0D")
        .replace('\n', "%0A");
    if property {
        escaped = escaped.replace(':', "%3A").replace(',', "%2C");
    }
    let cleaned = CONTROLS.replace_all(&escaped, "?");
    if cleaned.chars().nth(maximum).is_none() {
        return cleaned.into_owned();
    }
    if maximum == 0 {
        return String::new();
    }
    let mut prefix: String = cleaned.chars().take(maximum - 1).collect();
    if let Some(index) = prefix.rfind('%')
        && prefix.len() - index < 3
    {
        prefix.truncate(index);
    }
    prefix.push('…');
    prefix
}

// Full default folding is character-local. Uppercase followed by lowercase
// expands ligatures and normalizes final sigma. Unicode specifies distinct
// mappings for dotless i, capital sharp s, and the Cherokee script.
pub fn casefold(s: &str) -> String {
    if s.is_ascii() {
        return s.to_ascii_lowercase();
    }
    let mut folded = String::with_capacity(s.len());
    for ch in s.chars() {
        match ch {
            '\u{0131}' => folded.push(ch),
            '\u{1e9e}' => folded.push_str("ss"),
            '\u{13a0}'..='\u{13f5}' | '\u{13f8}'..='\u{13fd}' | '\u{ab70}'..='\u{abbf}' => {
                folded.extend(ch.to_uppercase())
            }
            _ => folded.extend(ch.to_uppercase().flat_map(char::to_lowercase)),
        }
    }
    folded
}
