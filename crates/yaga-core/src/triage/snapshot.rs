use crate::json::strict_value;
use regex::Regex;
use serde_json::Value;
use std::{collections::HashSet, sync::LazyLock};

pub const MAX_BYTES: usize = 8 << 20;
pub const MAX_FILES: usize = 3000;
pub(super) const BOUNDARY: [&str; 15] = [
    "host",
    "repository",
    "repository_id",
    "number",
    "state",
    "draft",
    "head_sha",
    "base_sha",
    "base_ref",
    "head_tree",
    "head_repository_id",
    "head_repository_fork",
    "author_id",
    "author_login",
    "author_type",
];
pub struct Snapshot(pub(super) Value);
fn keys(v: &Value, expected: &[&str], label: &str) -> Result<(), String> {
    let Some(d) = v.as_object() else {
        return Err(format!("invalid or incomplete triage {label}"));
    };
    if d.len() != expected.len() || !expected.iter().all(|&k| d.contains_key(k)) {
        return Err(format!("invalid or incomplete triage {label}"));
    }
    Ok(())
}
fn id(value: &Value, label: &str) -> Result<u64, String> {
    value
        .as_u64()
        .filter(|v| (1..=(1 << 53) - 1).contains(v))
        .ok_or_else(|| format!("invalid triage {label}"))
}
fn sha(v: &Value, label: &str) -> Result<(), String> {
    if v.as_str().is_some_and(|s| {
        s.len() == 40
            && s.bytes()
                .all(|b| b.is_ascii_digit() || matches!(b, b'a'..=b'f'))
    }) {
        Ok(())
    } else {
        Err(format!("invalid triage {label}"))
    }
}
fn path(v: &Value) -> Result<&str, String> {
    v.as_str()
        .filter(|s| {
            !s.is_empty()
                && s.len() <= 4096
                && !s.contains('\\')
                && !s.bytes().any(|b| b < 32 || b == 127)
                && s.split('/').count() <= 64
                && s.split('/').all(|p| !matches!(p, "" | "." | ".."))
        })
        .ok_or_else(|| "invalid triage repository-relative path".into())
}
pub(super) fn timestamp(v: &Value) -> Result<i64, String> {
    let invalid = "invalid triage review timestamp";
    let raw = v
        .as_str()
        .filter(|s| s.chars().count() <= 40 && s.ends_with('Z'))
        .ok_or(invalid)?;
    crate::timestamp::parse_time(raw)
        .flatten()
        .ok_or_else(|| invalid.into())
}
impl Snapshot {
    /// Decode only complete, explicitly bounded offline inventories. Object keys are unique at every depth.
    pub fn decode(raw: &[u8]) -> Result<Self, String> {
        if raw.len() > MAX_BYTES {
            return Err("triage snapshot exceeds 8 MiB".into());
        }
        Self::from_normalized_bytes(raw)
    }

    /// Live collection joins independently bounded pages. Escaped review bodies and
    /// changed paths can exceed the offline file cap while remaining within those bounds.
    pub fn from_normalized_bytes(raw: &[u8]) -> Result<Self, String> {
        if raw.len() > 64 * 1024 * 1024 {
            return Err("normalized triage snapshot exceeds its byte limit".into());
        }
        let doc = strict_value(raw).map_err(|e| {
            if e.to_string().contains("duplicate") {
                "triage snapshot contains duplicate JSON keys"
            } else {
                "invalid triage snapshot JSON"
            }
        })?;
        Self::parse(doc)
    }
    pub(super) fn parse(doc: Value) -> Result<Self, String> {
        let mut fields = BOUNDARY.to_vec();
        fields.extend([
            "version",
            "files",
            "changed_files",
            "reviews",
            "candidates",
            "candidate_limit",
            "candidates_examined",
        ]);
        keys(&doc, &fields, "snapshot")?;
        if doc["version"].as_u64() != Some(1) {
            return Err("triage snapshot version must be integer 1".into());
        }
        static HOST: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9.-]{0,252}$").unwrap());
        static REPO: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$").unwrap());
        static LOGIN: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9\[\]-]{0,99}$").unwrap());
        if !doc["host"].as_str().is_some_and(|s| HOST.is_match(s)) {
            return Err("invalid triage host".into());
        }
        if !doc["repository"].as_str().is_some_and(|s| REPO.is_match(s)) {
            return Err("invalid triage repository name".into());
        }
        for k in ["repository_id", "number", "head_repository_id", "author_id"] {
            id(&doc[k], k)?;
        }
        for k in ["head_sha", "base_sha", "head_tree"] {
            sha(&doc[k], k)?;
        }
        if !matches!(doc["state"].as_str(), Some("open" | "closed")) {
            return Err("invalid triage PR state".into());
        }
        if !doc["draft"].is_boolean() || !doc["head_repository_fork"].is_boolean() {
            return Err("invalid triage draft or repository fork flag".into());
        }
        if doc["head_repository_id"] != doc["repository_id"] && doc["head_repository_fork"] != true
        {
            return Err("inconsistent triage cross-repository provenance".into());
        }
        if !doc["base_ref"]
            .as_str()
            .is_some_and(|s| (1..=244).contains(&s.chars().count()) && !s.chars().any(|c| c < '!'))
        {
            return Err("invalid triage base branch".into());
        }
        if !doc["author_login"]
            .as_str()
            .is_some_and(|s| LOGIN.is_match(s))
        {
            return Err("invalid triage author login".into());
        }
        if !matches!(doc["author_type"].as_str(), Some("User" | "Bot")) {
            return Err("invalid triage author type".into());
        }
        let files = doc["files"]
            .as_array()
            .filter(|f| {
                f.len() <= MAX_FILES && doc["changed_files"].as_u64() == Some(f.len() as u64)
            })
            .ok_or("triage changed-file inventory is incomplete or over budget")?;
        let mut paths = HashSet::new();
        for f in files {
            keys(
                f,
                &["filename", "previous_filename", "sha", "status"],
                "changed file",
            )?;
            if !paths.insert(path(&f["filename"])?) {
                return Err("triage changed-file inventory contains duplicate paths".into());
            }
            sha(&f["sha"], "file SHA")?;
            match f["status"].as_str() {
                Some("renamed" | "copied") => {
                    path(&f["previous_filename"])?;
                }
                Some("added" | "removed" | "changed" | "modified") => {
                    if !f["previous_filename"].is_null() {
                        return Err("unexpected triage previous filename".into());
                    }
                }
                _ => return Err("invalid triage changed-file status".into()),
            }
        }
        let reviews = doc["reviews"]
            .as_array()
            .filter(|r| r.len() <= 99)
            .ok_or("triage reviews are incomplete or over budget")?;
        let mut seen = HashSet::new();
        for r in reviews {
            keys(
                r,
                &[
                    "id",
                    "user_id",
                    "user_type",
                    "state",
                    "commit_id",
                    "submitted_at",
                    "body",
                ],
                "review",
            )?;
            if !r["body"].as_str().is_some_and(|s| s.len() <= 65536) {
                return Err("invalid or oversized triage review body".into());
            }
            let rid = id(&r["id"], "review ID")?;
            id(&r["user_id"], "review author ID")?;
            if !seen.insert(rid) || !matches!(r["user_type"].as_str(), Some("User" | "Bot")) {
                return Err("ambiguous triage review identity".into());
            }
            if !matches!(
                r["state"].as_str(),
                Some("APPROVED" | "CHANGES_REQUESTED" | "DISMISSED" | "COMMENTED" | "PENDING")
            ) {
                return Err("invalid triage review state".into());
            }
            if !r["commit_id"].is_null() {
                sha(&r["commit_id"], "review commit")?;
            }
            if r["state"] != "PENDING" || !r["submitted_at"].is_null() {
                timestamp(&r["submitted_at"])?;
            }
        }
        let (limit, examined) = (
            doc["candidate_limit"].as_u64(),
            doc["candidates_examined"].as_u64(),
        );
        let Some((_, examined)) = limit.zip(examined).filter(|(l, e)| *l <= 20 && e <= l) else {
            return Err("invalid triage duplicate candidate scope".into());
        };
        let candidates = doc["candidates"]
            .as_array()
            .filter(|c| c.len() <= examined as usize)
            .ok_or("incomplete triage duplicate candidate inventory")?;
        seen.clear();
        for c in candidates {
            keys(
                c,
                &["number", "merge_commit_sha", "tree_sha"],
                "merged candidate",
            )?;
            let number = id(&c["number"], "merged PR number")?;
            if c["number"] == doc["number"] || !seen.insert(number) {
                return Err("duplicate triage merged PR candidate".into());
            }
            sha(&c["merge_commit_sha"], "merged commit")?;
            sha(&c["tree_sha"], "merged tree")?;
        }
        Ok(Self(doc))
    }
}
