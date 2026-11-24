use super::{
    markdown::{self, Document, Heading, Line},
    policy::DescriptionPolicy,
};
use crate::text::casefold;
use regex::Regex;
use serde::Serialize;
use std::{collections::HashSet, sync::LazyLock};

#[derive(Debug, Serialize)]
pub struct Diagnostic {
    pub code: &'static str,
    pub message: String,
    pub line: usize,
    pub column: usize,
}
#[derive(Debug, Default)]
pub struct Report {
    pub diagnostics: Vec<Diagnostic>,
    pub diagnostic_count: usize,
    pub word_count: usize,
    pub character_count: usize,
    pub spelling: String,
}
impl Report {
    pub fn add(
        &mut self,
        code: &'static str,
        message: impl Into<String>,
        line: usize,
        column: usize,
    ) {
        self.diagnostic_count += 1;
        if self.diagnostics.len() < 256 {
            self.diagnostics.push(Diagnostic {
                code,
                message: message.into(),
                line,
                column,
            });
        }
    }
}

fn hygiene(markup: &[String], mut emit: impl FnMut(&'static str, String, usize, usize)) {
    static FLAG: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\x{1f3f4}[\x{e0020}-\x{e007e}]{1,32}\x{e007f}").unwrap());
    static HIDDEN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[\p{Cf}\p{Zl}\p{Zp}\x{034f}\t]").unwrap());
    static ESCAPES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\\r\\n|\\n\\n").unwrap());
    static TICKS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"`+").unwrap());
    for (i, line) in markup.iter().enumerate() {
        let mut flags = FLAG.find_iter(line).peekable();
        for m in HIDDEN.find_iter(line) {
            while flags.peek().is_some_and(|f| f.end() <= m.start()) {
                flags.next();
            }
            let c = m.as_str().chars().next().unwrap();
            if c == '\t' {
                emit(
                    "description.tab",
                    "unexpected U+0009 TAB in prose; use spaces or put literal data in code".into(),
                    i + 1,
                    m.start() + 1,
                );
            } else if !matches!(c, '\u{200c}' | '\u{200d}')
                && !flags.peek().is_some_and(|f| f.start() < m.start())
            {
                let name = unicode_name(c);
                emit(
                    "description.invisible-character",
                    format!(
                        "unexpected U+{:04X} {name}; remove hidden formatting from prose",
                        c as u32
                    ),
                    i + 1,
                    m.start() + 1,
                );
            }
        }
        for m in ESCAPES.find_iter(line) {
            emit("description.escaped-newline","literal escaped line breaks in prose; write real newlines or quote the sequence as code".into(),i+1,m.start()+1);
        }
        for m in TICKS.find_iter(line) {
            emit(
                "description.backtick",
                "unmatched inline-code backtick delimiter; close it or escape a literal backtick"
                    .into(),
                i + 1,
                m.start() + 1,
            );
        }
    }
}

/// Name displayed for an invisible character in a source-located diagnostic.
pub fn unicode_name(c: char) -> String {
    unicode_names2::name(c)
        .map(|v| v.to_string())
        .unwrap_or_else(|| "UNNAMED CHARACTER".into())
}
fn validate_template(text: &str, policy: &DescriptionPolicy) -> Result<Document, String> {
    let doc = markdown::parse(
        &markdown::normalize(text).map_err(|e| format!("invalid description template: {e}"))?,
    );
    if !doc.issues.is_empty() {
        return Err("description template has an unclosed comment or fenced code block".into());
    }
    let mut first = None;
    hygiene(&doc.markup, |code, msg, _, _| {
        first.get_or_insert_with(|| format!("invalid description template: {code}: {msg}"));
    });
    if let Some(error) = first {
        return Err(error);
    }
    if !doc.tags.is_empty() {
        return Err(
            "description templates must use Markdown headings, not raw HTML heading tags".into(),
        );
    }
    if !(1..=64).contains(&doc.headings().count()) {
        return Err("description template requires 1 through 64 Markdown headings".into());
    }
    let mut seen = HashSet::new();
    for h in doc.headings() {
        if h.title.is_empty()
            || h.title.chars().count() > 256
            || h.title.chars().any(char::is_control)
        {
            return Err(
                "template headings require 1 through 256 characters without controls".into(),
            );
        }
        if !seen.insert((h.level, &h.title)) {
            return Err("description template contains duplicate heading identities".into());
        }
        if h.level < policy.heading_min_level {
            return Err("description template heading conflicts with heading-min-level".into());
        }
        if h.title.chars().count() > policy.heading_max_length {
            return Err("description template heading conflicts with heading-max-length".into());
        }
    }
    Ok(doc)
}
struct Placeholders {
    folded: HashSet<String>,
    max_chars: usize,
}

impl Placeholders {
    fn new(values: &[String]) -> Self {
        let folded: HashSet<_> = values.iter().map(|s| casefold(s)).collect();
        let max_chars = folded.iter().map(|s| s.chars().count()).max().unwrap_or(0);
        Self { folded, max_chars }
    }

    fn contains(&self, content: &str) -> bool {
        // Full case folding emits at least one scalar for each input scalar.
        // Long prose therefore cannot match a shorter folded placeholder.
        !self.folded.is_empty()
            && content.chars().nth(self.max_chars).is_none()
            && self.folded.contains(&casefold(content))
    }
}

fn words(line: &Line, placeholders: &Placeholders) -> usize {
    if line.unchecked || placeholders.contains(&line.content) {
        return 0;
    }
    line.content
        .split_whitespace()
        .filter(|w| {
            static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{L}\p{N}]").unwrap());
            WORD.is_match(w)
        })
        .count()
}
fn section<'a>(doc: &'a Document, h: &Heading) -> &'a [Line] {
    let end = doc
        .headings()
        .find(|next| next.line > h.line && next.level <= h.level)
        .map_or(doc.lines.len(), |next| next.line - 1);
    &doc.lines[h.end..end]
}
fn tokens<'a>(lines: &'a [Line], placeholders: &Placeholders) -> Vec<&'a str> {
    lines
        .iter()
        .filter(|l| words(l, placeholders) > 0)
        .flat_map(|l| l.content.split_whitespace())
        .collect()
}
pub type CheckError = (&'static str, String);
/// Check bounded body and optional explicit template. No filesystem or subprocess is used.
pub fn check_description(
    body: &str,
    policy: &DescriptionPolicy,
    template: Option<&str>,
) -> Result<Report, CheckError> {
    let text = markdown::normalize(body).map_err(|s| ("input", s))?;
    let doc = markdown::parse(&text);
    let expected = template
        .map(|t| validate_template(t, policy))
        .transpose()
        .map_err(|s| ("configuration", s))?;
    let placeholders = Placeholders::new(&policy.placeholders);
    let mut report = Report {
        character_count: text.chars().count(),
        word_count: doc.lines.iter().map(|l| words(l, &placeholders)).sum(),
        spelling: doc
            .lines
            .iter()
            .map(|l| l.visible.as_str())
            .collect::<Vec<_>>()
            .join("\n"),
        ..Report::default()
    };
    if report.word_count == 0 {
        report.add(
            "description.required",
            "description must contain meaningful prose beyond its headings",
            1,
            1,
        );
    } else if report.word_count < policy.min_words {
        report.add(
            "description.word-count",
            format!(
                "description has {} prose words; at least {} are required",
                report.word_count, policy.min_words
            ),
            1,
            1,
        );
    }
    if report.character_count > policy.max_length {
        report.add(
            "description.length",
            format!(
                "description has {} characters; at most {} are allowed",
                report.character_count, policy.max_length
            ),
            1,
            1,
        );
    }
    for &(n, msg) in &doc.issues {
        report.add("description.markdown", msg, n, 1);
    }
    for h in doc.headings() {
        if h.title.is_empty() {
            report.add(
                "description.heading-empty",
                "heading text must not be empty",
                h.line,
                1,
            );
        }
        if h.level < policy.heading_min_level {
            report.add(
                "description.heading-level",
                format!(
                    "H{} is not allowed; use H{} or deeper",
                    h.level, policy.heading_min_level
                ),
                h.line,
                1,
            );
        }
        let length = h.title.chars().count();
        if length > policy.heading_max_length {
            report.add("description.heading-length",format!("heading has {length} characters; at most {} are allowed; move details into a paragraph",policy.heading_max_length),h.line,1);
        }
    }
    for &(line, col) in &doc.tags {
        report.add(
            "description.html-heading",
            "raw HTML heading tags are not allowed; use Markdown section headings",
            line,
            col,
        );
    }
    hygiene(&doc.markup, |code, msg, n, col| {
        report.add(code, msg, n, col)
    });
    for (i, l) in doc.lines.iter().enumerate() {
        if l.heading.is_none() && placeholders.contains(&l.content) {
            report.add(
                "description.placeholder",
                "replace the placeholder with description content",
                i + 1,
                1,
            );
        }
        if l.unchecked && policy.require_checked_tasks {
            report.add(
                "description.unchecked-task",
                "description contains an unchecked task",
                i + 1,
                1,
            );
        }
    }
    if let Some(expected) = expected {
        let mut previous = 0;
        for h in expected.headings() {
            let label = format!("{} {}", "#".repeat(h.level), h.title);
            let mut matches = doc
                .headings()
                .filter(|item| item.title == h.title && item.level == h.level);
            let Some(actual) = matches.next() else {
                report.add(
                    "description.section-required",
                    format!("required template heading is missing: {label}"),
                    1,
                    1,
                );
                continue;
            };
            if let Some(duplicate) = matches.next() {
                report.add(
                    "description.section-duplicate",
                    format!("required template heading occurs more than once: {label}"),
                    duplicate.line,
                    1,
                );
            }
            if actual.line < previous {
                report.add(
                    "description.section-order",
                    format!("template heading is out of order: {label}"),
                    actual.line,
                    1,
                );
            }
            previous = actual.line;
            let content = tokens(section(&doc, actual), &placeholders);
            if content.is_empty() {
                report.add(
                    "description.section-empty",
                    format!("template section requires meaningful prose: {label}"),
                    actual.line,
                    1,
                );
            } else if content == tokens(section(&expected, h), &placeholders) {
                report.add(
                    "description.template-unchanged",
                    format!("replace the unchanged template content beneath: {label}"),
                    actual.line,
                    1,
                );
            }
        }
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn placeholder_prefilter_preserves_unicode_matches() {
        let policy = DescriptionPolicy {
            placeholders: vec!["ss".into(), "k".into()],
            ..DescriptionPolicy::default()
        };
        for (text, expected) in [("ß", true), ("SS", true), ("K", true), ("ßß", false)] {
            let result = check_description(text, &policy, None).unwrap();
            assert_eq!(
                result
                    .diagnostics
                    .iter()
                    .any(|d| d.code == "description.placeholder"),
                expected,
                "{text}"
            );
        }
    }
}
