//! Anchored, case-sensitive matching for repository-relative paths.
//!
//! Compile policy patterns once and pass one [`WorkBudget`] to every comparison
//! in a policy evaluation. All lengths charged as work are Unicode scalar counts.

use std::collections::HashSet;
use std::fmt;

pub mod changes;
pub mod modes;
mod policy;
pub mod portability;
pub mod records;
pub mod sizes;
#[cfg(test)]
mod tests;
pub mod tree;

pub const MAX_MATCH_WORK: usize = 10_000_000;
pub const MAX_PATH_BYTES: usize = 4096;
pub const MAX_PATTERN_BYTES: usize = 512;
pub const MAX_COMPONENTS: usize = 64;

/// A policy diagnostic displaying the offending value in the caller's language.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PolicyValueError {
    UnknownMode {
        field: String,
        name: String,
        at: String,
    },
    NewerVersion {
        key: String,
        version: i64,
        at: String,
    },
}

impl PolicyValueError {
    pub fn with_display_value(&self, value: impl fmt::Display) -> String {
        match self {
            Self::UnknownMode { field, at, .. } => {
                format!("{field} contains unknown mode {value}{at}")
            }
            Self::NewerVersion { key, at, .. } => format!(
                "{key} must be the integer 1{at} (found newer schema version {value}; update YAGA using its installation manager)"
            ),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChangeRuleIssue {
    Name,
    Pattern(Box<PathError>),
}

impl ChangeRuleIssue {
    /// The rule constructor's own diagnostic, including its path-pattern cause if present.
    pub fn details(&self) -> (String, Option<String>) {
        match self {
            Self::Name => (
                "change rule name must be a lowercase ASCII slug".into(),
                None,
            ),
            Self::Pattern(error) => {
                let path = error.to_string();
                (
                    path.replacen("path pattern", "change pattern", 1),
                    Some(path),
                )
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PathError {
    InvalidPath,
    InvalidPattern,
    PartialGlobstar,
    WorkLimit,
    Configuration(String),
    InvalidPolicyField {
        location: String,
        detail: String,
    },
    InvalidChangeRule {
        index: usize,
        at: String,
        issue: ChangeRuleIssue,
    },
    PolicyValue(PolicyValueError),
    Policy(&'static str),
    Selection(&'static str),
}

impl fmt::Display for PathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidChangeRule { index, at, issue } => match issue {
                ChangeRuleIssue::Name => write!(
                    f,
                    "rules[{index}].name must be a 1 through 64 character lowercase ASCII slug{at}"
                ),
                ChangeRuleIssue::Pattern(_) => {
                    write!(f, "invalid rules[{index}]{at}: {}", issue.details().0)
                }
            },
            Self::InvalidPolicyField { location, detail } => {
                write!(f, "invalid {location}: {detail}")
            }
            Self::PolicyValue(error) => f.write_str(&match error {
                PolicyValueError::UnknownMode { name, .. } => {
                    error.with_display_value(crate::text::python_repr(name))
                }
                PolicyValueError::NewerVersion { version, .. } => error.with_display_value(version),
            }),
            Self::InvalidPath => {
                f.write_str("path must be a canonical repository-relative POSIX path")
            }
            Self::InvalidPattern => {
                f.write_str("path pattern must be a canonical repository-relative POSIX pattern")
            }
            Self::PartialGlobstar => {
                f.write_str("path pattern ** wildcard must occupy an entire path component")
            }
            Self::WorkLimit => f.write_str("path evaluation exceeds the match-work limit"),
            Self::Policy(detail) | Self::Selection(detail) => f.write_str(detail),
            Self::Configuration(detail) => f.write_str(detail),
        }
    }
}

impl std::error::Error for PathError {}

#[derive(Debug)]
pub struct WorkBudget(usize);

impl Default for WorkBudget {
    fn default() -> Self {
        Self::new(MAX_MATCH_WORK)
    }
}

impl WorkBudget {
    pub fn new(limit: usize) -> Self {
        Self(limit.min(MAX_MATCH_WORK))
    }
    pub fn remaining(&self) -> usize {
        self.0
    }
    fn spend(&mut self, units: usize) -> Result<(), PathError> {
        self.0 = self.0.checked_sub(units).ok_or(PathError::WorkLimit)?;
        Ok(())
    }
}

// Unicode General_Category=Cf (unchanged in UCD 15 through 17). Cc and the
// two paragraph/line separators are handled separately; &str excludes surrogates.
fn unsafe_character(c: char) -> bool {
    c.is_control()
        || matches!(c as u32,
        0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x890..=0x891 |
        0x8e2 | 0x180e | 0x200b..=0x200f | 0x2028..=0x202e | 0x2060..=0x2064 |
        0x2066..=0x206f | 0xfeff | 0xfff9..=0xfffb | 0x110bd | 0x110cd |
        0x13430..=0x1343f | 0x1bca0..=0x1bca3 | 0x1d173..=0x1d17a |
        0xe0001 | 0xe0020..=0xe007f)
}

/// Units used to locate the second character when recognizing a Windows drive.
#[derive(Clone, Copy, Debug)]
pub enum DriveSyntax {
    UnicodeScalars,
    Utf16Units,
}

impl DriveSyntax {
    pub const NATIVE: Self = if cfg!(windows) {
        Self::Utf16Units
    } else {
        Self::UnicodeScalars
    };
}

fn portable(value: &str, syntax: DriveSyntax) -> bool {
    let mut chars = value.chars();
    let drive = chars
        .next()
        .is_some_and(|c| matches!(syntax, DriveSyntax::UnicodeScalars) || c.len_utf16() == 1)
        && chars.next() == Some(':');
    !drive && !value.contains('\\') && !value.chars().any(unsafe_character)
}

#[derive(Debug)]
struct Piece<'a> {
    text: &'a str,
    length: usize,
}

/// Validated components borrowed from an exact path, reusable across patterns.
#[derive(Debug)]
pub struct PathComponents<'a>(Vec<Piece<'a>>);

impl<'a> PathComponents<'a> {
    pub fn parse(value: &'a str) -> Result<Self, PathError> {
        Self::parse_with_drive_syntax(value, DriveSyntax::NATIVE)
    }

    pub fn parse_with_drive_syntax(value: &'a str, syntax: DriveSyntax) -> Result<Self, PathError> {
        if value.len() > MAX_PATH_BYTES || !portable(value, syntax) {
            return Err(PathError::InvalidPath);
        }
        Self::committed(value)
    }

    /// Git leaf grammar used by the mode and portability checks. Preserves
    /// control characters, drive-like prefixes, and literal backslashes.
    pub fn committed(value: &'a str) -> Result<Self, PathError> {
        if value.len() > MAX_PATH_BYTES || value.contains('\0') {
            return Err(PathError::InvalidPath);
        }
        let mut pieces = Vec::new();
        for text in value.split('/') {
            if pieces.len() == MAX_COMPONENTS || matches!(text, "" | "." | "..") {
                return Err(PathError::InvalidPath);
            }
            pieces.push(Piece {
                text,
                length: text.chars().count(),
            });
        }
        Ok(Self(pieces))
    }
}

#[derive(Debug, Clone)]
struct Literal {
    text: String,
    length: usize,
}

impl Literal {
    fn new(text: &str) -> Self {
        Self {
            text: text.into(),
            length: text.chars().count(),
        }
    }

    fn at(
        &self,
        value: &str,
        remaining: usize,
        exact: bool,
        work: &mut impl FnMut(usize) -> Result<(), PathError>,
    ) -> Result<bool, PathError> {
        work(1)?;
        if self.length > remaining || (exact && self.length != remaining) {
            return Ok(false);
        }
        for (a, b) in self.text.chars().zip(value.chars()) {
            work(1)?;
            if a != b {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

#[derive(Debug, Clone)]
enum Component {
    Globstar,
    Exact(Literal),
    Wildcard {
        prefix: Option<Literal>,
        middle: Vec<Literal>,
        suffix: Option<Literal>,
    },
}

impl Component {
    fn compile(piece: &str) -> Result<Self, PathError> {
        if piece == "**" {
            return Ok(Self::Globstar);
        }
        if piece.contains("**") {
            return Err(PathError::PartialGlobstar);
        }
        if !piece.contains('*') {
            return Ok(Self::Exact(Literal::new(piece)));
        }
        let mut segments = piece.split('*');
        let prefix = segments.next().filter(|s| !s.is_empty()).map(Literal::new);
        let suffix = segments
            .next_back()
            .filter(|s| !s.is_empty())
            .map(Literal::new);
        let middle = segments.map(Literal::new).collect();
        Ok(Self::Wildcard {
            prefix,
            middle,
            suffix,
        })
    }

    fn matches(
        &self,
        value: &Piece<'_>,
        work: &mut impl FnMut(usize) -> Result<(), PathError>,
    ) -> Result<bool, PathError> {
        match self {
            Self::Globstar => unreachable!("globstars match whole components"),
            Self::Exact(literal) => literal.at(value.text, value.length, true, work),
            Self::Wildcard {
                prefix,
                middle,
                suffix,
            } => {
                let mut text = value.text;
                let mut length = value.length;
                if let Some(prefix) = prefix {
                    if !prefix.at(text, length, false, work)? {
                        return Ok(false);
                    }
                    text = &text[prefix.text.len()..];
                    length -= prefix.length;
                }
                if let Some(suffix) = suffix {
                    if length < suffix.length {
                        return Ok(false);
                    }
                    let (suffix_start, _) =
                        text.char_indices().rev().nth(suffix.length - 1).unwrap();
                    if !suffix.at(&text[suffix_start..], suffix.length, false, work)? {
                        return Ok(false);
                    }
                    text = &text[..suffix_start];
                }
                for segment in middle {
                    let Some(index) = text.find(&segment.text) else {
                        work(text.chars().count().max(1))?;
                        return Ok(false);
                    };
                    work(text[..index].chars().count() + segment.length)?;
                    text = &text[index + segment.text.len()..];
                }
                Ok(true)
            }
        }
    }
}

#[derive(Debug, Clone)]
pub struct Pattern {
    source: String,
    components: Vec<Component>,
}

impl Pattern {
    pub fn compile(value: &str) -> Result<Self, PathError> {
        Self::compile_with_drive_syntax(value, DriveSyntax::NATIVE)
    }

    pub fn compile_with_drive_syntax(value: &str, syntax: DriveSyntax) -> Result<Self, PathError> {
        if value.len() > MAX_PATTERN_BYTES {
            return Err(PathError::InvalidPattern);
        }
        let path = PathComponents::parse_with_drive_syntax(value, syntax)
            .map_err(|_| PathError::InvalidPattern)?;
        let components = path
            .0
            .iter()
            .map(|p| Component::compile(p.text))
            .collect::<Result<_, _>>()?;
        Ok(Self {
            source: value.into(),
            components,
        })
    }

    pub fn as_str(&self) -> &str {
        &self.source
    }

    pub fn matches(
        &self,
        path: &PathComponents<'_>,
        work: &mut WorkBudget,
    ) -> Result<bool, PathError> {
        self.matches_with(path, &mut |units| work.spend(units))
    }

    /// Call the observer for each individual fuel charge, retaining the same
    /// ordering and early exit as a shared work budget.
    pub fn matches_with(
        &self,
        path: &PathComponents<'_>,
        work: &mut impl FnMut(usize) -> Result<(), PathError>,
    ) -> Result<bool, PathError> {
        let count = path.0.len();
        let mut previous = [false; MAX_COMPONENTS + 1];
        previous[0] = true;
        for component in &self.components {
            work(count)?;
            let mut current = [false; MAX_COMPONENTS + 1];
            if matches!(component, Component::Globstar) {
                current[0] = previous[0];
                for i in 1..=count {
                    current[i] = previous[i] || current[i - 1];
                }
            } else {
                for (i, piece) in path.0.iter().enumerate() {
                    current[i + 1] = previous[i] && component.matches(piece, work)?;
                }
            }
            previous = current;
        }
        Ok(previous[count])
    }
}

pub fn match_pattern(pattern: &str, path: &str) -> Result<bool, PathError> {
    Pattern::compile(pattern)?.matches(&PathComponents::parse(path)?, &mut WorkBudget::default())
}

fn unique(values: &[&str]) -> bool {
    let mut seen = HashSet::new();
    values.iter().all(|value| seen.insert(*value))
}

fn sorted_selection(paths: &[&str], maximum: usize) -> Result<(), PathError> {
    if paths.len() > maximum {
        return Err(PathError::Selection(
            "path selection exceeds the entry limit",
        ));
    }
    if paths.iter().any(|path| path.len() > MAX_PATH_BYTES) {
        return Err(PathError::InvalidPath);
    }
    if !paths.windows(2).all(|pair| pair[0] < pair[1]) {
        return Err(PathError::Selection(
            "selected paths must be unique and sorted",
        ));
    }
    Ok(())
}

fn leaf_topology(paths: &[&str]) -> Result<(), PathError> {
    for path in paths {
        let prefix = format!("{path}/");
        let index = paths.partition_point(|&p| p < prefix.as_str());
        if paths.get(index).is_some_and(|p| p.starts_with(&prefix)) {
            return Err(PathError::Selection(
                "committed leaf paths cannot contain another leaf path",
            ));
        }
    }
    Ok(())
}

/// Ordered first-match overrides shared by blob sizes and entry modes.
#[derive(Debug)]
struct Overrides<T>(Vec<(Pattern, T)>);

impl<T> Overrides<T> {
    fn new<U>(
        values: &[(&str, U)],
        syntax: DriveSyntax,
        validate: impl Fn(&U) -> Result<T, PathError>,
    ) -> Result<Self, PathError> {
        if values.len() > 128 {
            return Err(PathError::Policy("policy exceeds 128 path overrides"));
        }
        let mut seen = HashSet::new();
        let mut rules = Vec::with_capacity(values.len());
        for (source, value) in values {
            let pattern = Pattern::compile_with_drive_syntax(source, syntax)?;
            if !seen.insert(*source) {
                return Err(PathError::Policy("path overrides must be unique"));
            }
            rules.push((pattern, validate(value)?));
        }
        Ok(Self(rules))
    }

    fn select<'a>(
        &'a self,
        path: &PathComponents<'_>,
        default: &'a T,
        work: &mut WorkBudget,
    ) -> Result<(Option<&'a str>, &'a T), PathError> {
        for (pattern, value) in &self.0 {
            if pattern.matches(path, work)? {
                return Ok((Some(pattern.as_str()), value));
            }
        }
        Ok((None, default))
    }
}
