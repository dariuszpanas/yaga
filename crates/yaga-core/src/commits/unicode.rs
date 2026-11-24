pub(super) use crate::text::{casefold as fold, python_repr as repr};
use regex::Regex;
use std::collections::HashMap;

/// Character data supplied when an embedding interpreter uses a different Unicode release.
#[derive(Clone, Debug)]
pub struct ScalarProperties {
    pub category: String,
    pub folded: String,
    pub unchanged_lower: bool,
    pub unchanged_upper: bool,
    pub lowercase: bool,
}

#[derive(Default)]
pub struct CommitUnicode(pub HashMap<char, ScalarProperties>);

impl CommitUnicode {
    /// Apply a single-character class to contiguous native spans and host overrides.
    pub(super) fn contains(
        &self,
        value: &str,
        class: &Regex,
        predicate: impl Fn(char, &str) -> bool,
    ) -> bool {
        if self.0.is_empty() || value.is_ascii() {
            return class.is_match(value);
        }
        let mut start = 0;
        for (index, c) in value.char_indices() {
            if let Some(properties) = self.0.get(&c) {
                if class.is_match(&value[start..index]) || predicate(c, &properties.category) {
                    return true;
                }
                start = index + c.len_utf8();
            }
        }
        class.is_match(&value[start..])
    }

    pub(super) fn fold(&self, value: &str) -> String {
        if self.0.is_empty() || value.is_ascii() {
            return fold(value);
        }
        let mut result = String::with_capacity(value.len());
        let mut start = 0;
        for (index, c) in value.char_indices() {
            if let Some(properties) = self.0.get(&c) {
                result.push_str(&fold(&value[start..index]));
                result.push_str(&properties.folded);
                start = index + c.len_utf8();
            }
        }
        result.push_str(&fold(&value[start..]));
        result
    }

    pub(super) fn case_unchanged(&self, value: &str, lower: bool) -> bool {
        if self.0.is_empty() || value.is_ascii() {
            return if lower {
                value.to_lowercase() == value
            } else {
                value.to_uppercase() == value
            };
        }
        value.chars().all(|c| match self.0.get(&c) {
            Some(properties) if lower => properties.unchanged_lower,
            Some(properties) => properties.unchanged_upper,
            None if lower => c.to_lowercase().eq([c]),
            None => c.to_uppercase().eq([c]),
        })
    }

    pub(super) fn repr(&self, value: &str) -> String {
        if self.0.is_empty() || value.is_ascii() {
            return repr(value);
        }
        crate::text::python_repr_with(value, |c| {
            self.0.get(&c).map(|p| p.category.starts_with(['C', 'Z']))
        })
    }
}

pub(super) fn is_space(ch: char) -> bool {
    ch.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&ch)
}
