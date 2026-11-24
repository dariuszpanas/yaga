//! Strict schema-v1 policy for pull-request descriptions.
use crate::{
    commits::TyposPolicy,
    fs::{read_regular_prefix, resolve_user_path},
    text::{casefold, safe_text},
    version::{REQUIREMENT_TYPE_ERROR, check_requirement},
};
use regex::Regex;
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    sync::LazyLock,
};
use toml::Table;

pub const MAX_DESCRIPTION_POLICY_BYTES: usize = 65536;

#[derive(Clone, Debug, serde::Serialize)]
pub struct DescriptionPolicy {
    pub min_words: usize,
    pub max_length: usize,
    pub placeholders: Vec<String>,
    pub require_checked_tasks: bool,
    pub typos: TyposPolicy,
    pub heading_min_level: usize,
    pub heading_max_length: usize,
}
impl Default for DescriptionPolicy {
    fn default() -> Self {
        Self {
            min_words: 1,
            max_length: 65536,
            placeholders: vec!["TODO".into(), "TBD".into()],
            require_checked_tasks: false,
            typos: TyposPolicy::Skip,
            heading_min_level: 2,
            heading_max_length: 120,
        }
    }
}

pub fn parse_description_policy(raw: &[u8]) -> Result<DescriptionPolicy, String> {
    if raw.len() > MAX_DESCRIPTION_POLICY_BYTES {
        return Err("description policy exceeds the 65536-byte limit".into());
    }
    let invalid = "description policy is not valid UTF-8 TOML";
    let text = std::str::from_utf8(raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw))
        .map_err(|_| invalid)?;
    let doc: Table = toml::from_str(text).map_err(|_| invalid)?;
    parse_description_table(&doc)
}

/// Validate an existing TOML table using the same domain rules as the file loader.
pub fn parse_description_table(doc: &Table) -> Result<DescriptionPolicy, String> {
    if let Some(v) = doc.get("required-version") {
        check_requirement(v.as_str().ok_or(REQUIREMENT_TYPE_ERROR)?)?;
    }
    let mut unknown: Vec<_> = doc
        .keys()
        .filter(|k| {
            !matches!(
                k.as_str(),
                "description-policy-version"
                    | "min-words"
                    | "max-length"
                    | "placeholders"
                    | "require-checked-tasks"
                    | "typos"
                    | "heading-min-level"
                    | "heading-max-length"
                    | "required-version"
            )
        })
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(format!(
            "unknown description policy key(s): {}",
            safe_text(&unknown.join(", "), 300)
        ));
    }
    let ver = doc
        .get("description-policy-version")
        .ok_or("description policy requires description-policy-version")?
        .as_integer();
    if let Some(v) = ver.filter(|v| *v > 1) {
        return Err(format!(
            "description-policy-version must be the integer 1 (found newer schema version {v}; update YAGA using its installation manager)"
        ));
    }
    if ver != Some(1) {
        return Err(
            "invalid description policy: description policy version must be the integer 1".into(),
        );
    }
    let integer = |key: &str, default, min, max| -> Result<usize, String> {
        let Some(v) = doc.get(key) else {
            return Ok(default);
        };
        v.as_integer()
            .and_then(|n| usize::try_from(n).ok())
            .filter(|n| (min..=max).contains(n))
            .ok_or_else(|| {
                format!(
                    "invalid description policy: {key} must be an integer from {min} through {max}"
                )
            })
    };
    let mut policy = DescriptionPolicy::default();
    policy.min_words = integer("min-words", policy.min_words, 0, 65536)?;
    policy.max_length = integer("max-length", policy.max_length, 1, 65536)?;
    if policy.min_words.saturating_mul(2).saturating_sub(1) > policy.max_length {
        return Err("invalid description policy: min-words cannot fit within max-length".into());
    }
    if let Some(v) = doc.get("require-checked-tasks") {
        policy.require_checked_tasks = v
            .as_bool()
            .ok_or("invalid description policy: require-checked-tasks must be a boolean")?;
    }
    if let Some(v) = doc.get("typos") {
        policy.typos = match v.as_str() {
            Some("skip") => TyposPolicy::Skip,
            Some("check") => TyposPolicy::Check,
            _ => return Err("invalid description policy: typos must be 'skip' or 'check'".into()),
        };
    }
    policy.heading_min_level = integer("heading-min-level", policy.heading_min_level, 1, 6)?;
    policy.heading_max_length = integer("heading-max-length", policy.heading_max_length, 1, 256)?;
    if let Some(values) = doc.get("placeholders") {
        let values = values
            .as_array()
            .ok_or("description policy placeholders must be an array of strings")?;
        if values.len() > 128 {
            return Err(
                "invalid description policy: placeholders must contain at most 128 strings".into(),
            );
        }
        static UNSAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Cf}]").unwrap());
        let mut seen = HashSet::new();
        policy.placeholders.clear();
        for v in values {
            let s=v.as_str().filter(|s| !s.is_empty() && s.trim()==*s && s.chars().count()<=128 && !UNSAFE.is_match(s))
                .ok_or("invalid description policy: placeholders must be nonempty trimmed single-line strings of at most 128 characters")?;
            if !seen.insert(casefold(s)) {
                return Err(
                    "invalid description policy: placeholders must be unique ignoring case".into(),
                );
            }
            policy.placeholders.push(s.into());
        }
    }
    Ok(policy)
}

pub fn load_description_policy(path: &Path) -> Result<(DescriptionPolicy, PathBuf), String> {
    let error = "description policy could not be read as a regular file";
    let path = resolve_user_path(path).map_err(|_| error)?;
    let bytes = read_regular_prefix(&path, MAX_DESCRIPTION_POLICY_BYTES).map_err(|_| error)?;
    Ok((parse_description_policy(&bytes)?, path))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn schema_bounds_and_normalization() {
        let root = "description-policy-version=1\n";
        let defaults = parse_description_policy(root.as_bytes()).unwrap();
        assert_eq!(
            (defaults.heading_min_level, defaults.heading_max_length),
            (2, 120)
        );
        assert_eq!(defaults.placeholders, ["TODO", "TBD"]);
        for invalid in [
            "heading-min-level=true",
            "heading-min-level=7",
            "heading-max-length=257",
            "typos='ok'",
            "min-words=8\nmax-length=5",
            "placeholders=['Straße','STRASSE']",
            "placeholders=[' x']",
            "placeholders=[2001-01-01]",
        ] {
            assert!(
                parse_description_policy(format!("{root}{invalid}").as_bytes()).is_err(),
                "{invalid}"
            );
        }
        assert!(parse_description_policy(&vec![b' '; 65537]).is_err());
    }
}
