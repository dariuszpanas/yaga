use std::borrow::Cow;
use std::collections::HashSet;
use std::fs::File;
use std::io::Read;
use std::path::Path;

use super::{
    BranchError, BranchPolicy, LoadedBranchPolicy, MAX_BRANCH_PATTERNS, MAX_BRANCH_POLICY_BYTES,
};
use crate::fs::resolve_user_path;
use crate::version::{REQUIREMENT_TYPE_ERROR, check_requirement};

pub(super) fn path_text(path: &Path) -> Cow<'_, str> {
    path.to_string_lossy()
}

fn display_path(path: &Path) -> String {
    super::DisplayField::Safe.render(&path_text(path), 300)
}

/// Parse policy bytes in memory. The supplied path is used only for diagnostic display.
pub fn parse_branch_policy(bytes: &[u8], path: &Path) -> Result<BranchPolicy, BranchError> {
    let path = display_path(path);
    let failed = BranchError::Configuration;
    if bytes.len() > MAX_BRANCH_POLICY_BYTES {
        return Err(failed(format!(
            "branch policy exceeds {MAX_BRANCH_POLICY_BYTES} bytes: {path}"
        )));
    }
    let bytes = bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes);
    let document = std::str::from_utf8(bytes)
        .map_err(|_| failed(format!("branch policy is not valid UTF-8: {path}")))?;
    let document: toml::Table = toml::from_str(document).map_err(|error| {
        failed(format!(
            "invalid branch policy TOML in {path}: {}",
            error.message()
        ))
    })?;

    parse_branch_table(&document, &path)
}

/// Normalize an already decoded policy, retaining the host parser's grammar.
pub fn parse_branch_table(document: &toml::Table, path: &str) -> Result<BranchPolicy, BranchError> {
    let failed = BranchError::Configuration;
    if let Some(value) = document.get("required-version") {
        let text = value
            .as_str()
            .ok_or_else(|| failed(REQUIREMENT_TYPE_ERROR.into()))?;
        check_requirement(text).map_err(failed)?;
    }
    let mut unknown = document
        .keys()
        .map(String::as_str)
        .filter(|key| {
            !matches!(
                *key,
                "branch-policy-version" | "allowed-patterns" | "required-version"
            )
        })
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(failed(format!(
            "unknown branch policy key(s) in {path}: {}",
            unknown.join(", ")
        )));
    }
    let missing = ["allowed-patterns", "branch-policy-version"]
        .into_iter()
        .filter(|key| !document.contains_key(*key))
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return Err(failed(format!(
            "branch policy is missing required key(s) in {path}: {}",
            missing.join(", ")
        )));
    }
    let version = document["branch-policy-version"].as_integer();
    if version != Some(1) {
        let mut message = format!("branch-policy-version must be the integer 1 in {path}");
        if let Some(value) = version.filter(|v| *v > 1) {
            message.push_str(&format!(
                " (found newer schema version {value}; update YAGA using its installation manager)"
            ));
        }
        return Err(failed(message));
    }
    let wrong_type = || {
        failed(format!(
            "allowed-patterns must be an array of strings in {path}"
        ))
    };
    let patterns = document["allowed-patterns"]
        .as_array()
        .ok_or_else(wrong_type)?;
    if !patterns.iter().all(|p| p.is_str()) {
        return Err(wrong_type());
    }
    if patterns.is_empty() {
        return Err(failed(format!(
            "allowed-patterns must not be empty in {path}"
        )));
    }
    if patterns.len() > MAX_BRANCH_PATTERNS {
        return Err(failed(format!(
            "allowed-patterns exceeds {MAX_BRANCH_PATTERNS} entries in {path}"
        )));
    }
    let patterns: Vec<_> = patterns.iter().filter_map(toml::Value::as_str).collect();
    if patterns.iter().collect::<HashSet<_>>().len() != patterns.len() {
        return Err(failed(format!("allowed-patterns must be unique in {path}")));
    }
    BranchPolicy::new(patterns.into_iter().map(str::to_owned).collect())
        .map_err(|error| failed(format!("invalid branch policy in {path}: {error}")))
}

/// Read one explicit, bounded policy file and retain its resolved path.
pub fn load_branch_policy(path: &Path) -> Result<LoadedBranchPolicy, BranchError> {
    let path = resolve_user_path(path).map_err(|_| {
        let path = path_text(path);
        let label = crate::fs::display_path(Path::new(path.as_ref()));
        BranchError::Configuration(format!(
            "cannot resolve branch policy {}",
            super::DisplayField::Safe.render(&label, 300)
        ))
    })?;
    let mut bytes = Vec::new();
    File::open(&path)
        .and_then(|file| {
            file.take(MAX_BRANCH_POLICY_BYTES as u64 + 1)
                .read_to_end(&mut bytes)
        })
        .map_err(|_| {
            BranchError::Configuration(format!("cannot read branch policy {}", display_path(&path)))
        })?;
    let policy = parse_branch_policy(&bytes, &path)?;
    Ok(LoadedBranchPolicy { policy, path })
}
