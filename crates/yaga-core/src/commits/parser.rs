use regex::Regex;
use serde::Serialize;
use std::{borrow::Cow, sync::LazyLock};

use super::{CommitUnicode, FooterSyntax, MessageFormat, unicode::is_space};

pub const MAX_MESSAGE_BYTES: usize = 1024 * 1024;

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ParsedCommit<Text = String> {
    pub message: Text,
    pub header: Text,
    pub commit_type: Option<Text>,
    pub scope: Option<Text>,
    pub description: Text,
    pub breaking: bool,
    pub breaking_header: bool,
    pub breaking_footer: bool,
    pub separator_valid: bool,
    pub body: Text,
    pub body_lines: Vec<Text>,
    pub body_start_line: usize,
    pub footer_lines: Vec<Text>,
    pub footer_start_line: Option<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CommitFooter<'a> {
    pub token: &'a str,
    pub separator: char,
    pub line: usize,
}

pub fn normalize_message(message: &str) -> String {
    normalized_text(message).into_owned()
}

pub(super) fn normalized_text(message: &str) -> Cow<'_, str> {
    if memchr::memchr(b'\r', message.as_bytes()).is_none() {
        return Cow::Borrowed(message.trim_end_matches('\n'));
    }
    let mut normalized = message.replace("\r\n", "\n").replace('\r', "\n");
    normalized.truncate(normalized.trim_end_matches('\n').len());
    Cow::Owned(normalized)
}

/// Recognize one token start. Once a footer block starts, all following lines
/// belong to it, including blank lines and unindented continuation lines.
pub fn footer_start(line: &str, number: usize, syntax: FooterSyntax) -> Option<CommitFooter<'_>> {
    let len = if line.starts_with("BREAKING CHANGE") {
        "BREAKING CHANGE".len()
    } else {
        line.bytes()
            .take_while(|ch| ch.is_ascii_alphanumeric() || *ch == b'-')
            .count()
    };
    if len == 0 {
        return None;
    }
    let (token, tail) = line.split_at(len);
    let (separator, value) = if let Some(value) = tail.strip_prefix(" #") {
        ('#', value)
    } else {
        let value = tail.strip_prefix(':')?;
        let value = match syntax {
            FooterSyntax::Conventional => value.strip_prefix(' ')?,
            FooterSyntax::ColonWhitespace => {
                let trimmed = value.trim_start_matches(is_space);
                if trimmed.len() == value.len() {
                    return None;
                }
                trimmed
            }
        };
        (':', value)
    };
    value.chars().next().filter(|ch| !is_space(*ch))?;
    Some(CommitFooter {
        token,
        separator,
        line: number,
    })
}

fn valid_token(s: &str, unicode: &CommitUnicode) -> bool {
    static UNSAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Cf}]").unwrap());
    !s.is_empty()
        && !s.chars().any(is_space)
        && !unicode.contains(s, &UNSAFE, |_, category| matches!(category, "Cc" | "Cf"))
}

fn plain_header(header: &str, unicode: &CommitUnicode) -> bool {
    static UNSAFE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Zl}\p{Zp}[\p{Cf}&&[^\u200c\u200d]]]").unwrap());
    !header.is_empty()
        && header == header.trim_matches(is_space)
        && !unicode.contains(header, &UNSAFE, |c, category| {
            matches!(category, "Cc" | "Zl" | "Zp")
                || (category == "Cf" && !matches!(c, '\u{200c}' | '\u{200d}'))
        })
}

struct Header<'a> {
    commit_type: Option<&'a str>,
    scope: Option<&'a str>,
    description: &'a str,
    breaking: bool,
}

fn parse_header<'a>(
    header: &'a str,
    format: MessageFormat,
    unicode: &CommitUnicode,
) -> Option<Header<'a>> {
    if format == MessageFormat::Plain {
        return plain_header(header, unicode).then_some(Header {
            commit_type: None,
            scope: None,
            description: header,
            breaking: false,
        });
    }
    let (prefix, description) = header.split_once(": ")?;
    if description.is_empty() || description.trim_matches(is_space) != description {
        return None;
    }
    let components = prefix.strip_suffix('!').unwrap_or(prefix);
    let (kind, scope) = match components.split_once('(') {
        Some((kind, scope)) => {
            let scope = scope.strip_suffix(')')?;
            if scope.contains(['(', ')']) || !valid_token(scope, unicode) {
                return None;
            }
            (kind, Some(scope))
        }
        None => (components, None),
    };
    if kind.contains(['!', ':', ')']) || !valid_token(kind, unicode) {
        return None;
    }
    Some(Header {
        commit_type: Some(kind),
        scope,
        description,
        breaking: components != prefix,
    })
}

/// Parse a UTF-8 message independently of policy. Source adapters enforce the
/// input byte ceiling before reading external data. The checker also enforces
/// the normalized size before parsing.
pub fn parse_message(
    message: &str,
    format: MessageFormat,
    syntax: FooterSyntax,
) -> Option<ParsedCommit> {
    parse_normalized(
        &normalized_text(message),
        format,
        syntax,
        true,
        &CommitUnicode::default(),
    )
}

/// Use the same parser for the owned public document and the borrowed view
/// used while checking. The latter points directly into the normalized input.
pub(super) fn parse_normalized<'a, Text: From<&'a str>>(
    message: &'a str,
    format: MessageFormat,
    syntax: FooterSyntax,
    collect_body_lines: bool,
    unicode: &CommitUnicode,
) -> Option<ParsedCommit<Text>> {
    let (header, tail) = message.split_once('\n').unwrap_or((message, ""));
    let fields = parse_header(header, format, unicode)?;
    let (content, has_separator) = match tail.strip_prefix('\n') {
        Some(content) => (content, true),
        None => (tail, false),
    };
    let body_start_line = if has_separator { 3 } else { 2 };
    let footer = footer_offset(content, syntax);
    let (body, footers) = content.split_at(footer.unwrap_or(content.len()));
    let footer_start_line =
        footer.map(|_| body_start_line + body.bytes().filter(|&byte| byte == b'\n').count());
    let body = body.trim_end_matches('\n');
    let breaking_footer = footers.lines().any(|line| {
        footer_start(line, 1, syntax).is_some_and(|f| {
            f.separator == ':' && matches!(f.token, "BREAKING CHANGE" | "BREAKING-CHANGE")
        })
    });
    Some(ParsedCommit {
        header: header.into(),
        commit_type: fields.commit_type.map(Into::into),
        scope: fields.scope.map(Into::into),
        description: fields.description.into(),
        breaking: fields.breaking || breaking_footer,
        breaking_header: fields.breaking,
        breaking_footer,
        separator_valid: header.len() == message.len() || has_separator,
        body: body.trim_start_matches('\n').into(),
        body_lines: if collect_body_lines {
            body.lines().map(Into::into).collect()
        } else {
            Vec::new()
        },
        body_start_line,
        footer_lines: footers.lines().map(Into::into).collect(),
        footer_start_line,
        message: message.into(),
    })
}

/// A footer begins at the first line of the body or after a blank line.
/// Skip whole paragraphs and compute the source line only if a footer exists.
fn footer_offset(content: &str, syntax: FooterSyntax) -> Option<usize> {
    let mut paragraph = content.trim_start_matches('\n');
    loop {
        let first_line = paragraph.split('\n').next().unwrap_or_default();
        if footer_start(first_line, 1, syntax).is_some() {
            return Some(content.len() - paragraph.len());
        }
        let blank_line = memchr::memmem::find(paragraph.as_bytes(), b"\n\n")?;
        paragraph = paragraph[blank_line + 2..].trim_start_matches('\n');
    }
}
