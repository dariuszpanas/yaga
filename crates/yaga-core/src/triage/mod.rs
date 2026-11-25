//! Bounded trust triage and current-head admission over validated metadata.
#[cfg(feature = "process")]
mod publication;
#[cfg(feature = "process")]
mod remote;
mod snapshot;
#[cfg(test)]
mod tests;
#[cfg(feature = "process")]
pub use remote::{check_live, collect_for_review};
#[cfg(feature = "process")]
pub fn load_policy(path: &std::path::Path) -> Result<TriagePolicy, String> {
    let data = crate::fs::resolve_user_path(path)
        .and_then(|p| crate::fs::read_regular_prefix(&p, 65536))
        .map_err(|_| "cannot read explicit pull-request triage policy")?;
    TriagePolicy::parse(&data)
}
use crate::{
    paths::{DriveSyntax, PathComponents, Pattern, WorkBudget},
    policies::triage::TriagePolicy,
};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
pub use snapshot::{MAX_BYTES, MAX_FILES, Snapshot};
use std::{collections::HashMap, fmt::Write};

const MARKER: &str = "<!-- yaga-pr-triage-acceptance:v1 ";

/// Escape a serialized JSON document for the existing ASCII report and digest wire format.
pub fn ascii_json(serialized: &str) -> String {
    let mut out = String::with_capacity(serialized.len());
    for c in serialized.chars() {
        if c.is_ascii() && c != '\u{7f}' {
            out.push(c);
        } else {
            for unit in c.encode_utf16(&mut [0; 2]) {
                write!(out, "\\u{unit:04x}").unwrap();
            }
        }
    }
    out
}
fn acknowledgment(policy: &TriagePolicy, doc: &Value) -> String {
    let mut b = Map::new();
    for k in [
        "host",
        "repository_id",
        "number",
        "head_sha",
        "base_sha",
        "base_ref",
    ] {
        b.insert(k.into(), doc[k].clone());
    }
    b.insert("policy_digest".into(), policy.digest.clone().into());
    b.sort_keys();
    let raw = ascii_json(&serde_json::to_string(&b).unwrap());
    format!("{MARKER}{:x} -->", Sha256::digest(raw.as_bytes()))
}
fn match_any(patterns: &[Pattern], name: &str, budget: &mut WorkBudget) -> Result<bool, String> {
    let path =
        PathComponents::committed(name).map_err(|_| "invalid triage repository-relative path")?;
    for p in patterns {
        if p.matches(&path, budget)
            .map_err(|_| "triage path matching budget exhausted")?
        {
            return Ok(true);
        }
    }
    Ok(false)
}
fn compile(values: &[String], syntax: DriveSyntax) -> Result<Vec<Pattern>, String> {
    values
        .iter()
        .map(|p| {
            Pattern::compile_with_drive_syntax(p, syntax)
                .map_err(|_| "invalid triage policy path pattern".into())
        })
        .collect()
}
/// Evaluate an exact metadata snapshot. Publishing a result requires fresh, independently validated remote state.
pub fn evaluate(policy: &TriagePolicy, snapshot: &Snapshot) -> Result<Value, String> {
    evaluate_with_drive_syntax(policy, snapshot, DriveSyntax::NATIVE)
}

pub fn evaluate_with_drive_syntax(
    policy: &TriagePolicy,
    snapshot: &Snapshot,
    syntax: DriveSyntax,
) -> Result<Value, String> {
    let doc = &snapshot.0;
    if doc["host"] != policy.host || doc["repository_id"] != policy.repository_id {
        return Err("triage policy host/repository identity does not match PR".into());
    }
    if doc["candidate_limit"] != policy.duplicate_candidates {
        return Err("triage duplicate inventory does not match policy scope".into());
    }
    let files = doc["files"].as_array().unwrap();
    let reviews = doc["reviews"].as_array().unwrap();
    let candidates = doc["candidates"].as_array().unwrap();
    let mut budget = WorkBudget::default();
    let sensitive = compile(&policy.sensitive_patterns, syntax)?;
    let mut findings = Vec::new();
    for file in files {
        let name = file["filename"].as_str().unwrap();
        for path in
            std::iter::once(name).chain(file["previous_filename"].as_str().filter(|&s| s != name))
        {
            if match_any(&sensitive, path, &mut budget)? {
                findings.push(json!({"code":"triage.sensitive-path","path":path}));
            }
        }
    }
    for c in candidates {
        if c["tree_sha"] == doc["head_tree"] {
            findings.push(json!({"code":"triage.merged-tree-match","pull_request_number":c["number"],"merge_commit_sha":c["merge_commit_sha"]}));
        }
    }
    let author = doc["author_id"].as_u64().unwrap();
    let same = doc["head_repository_id"] == doc["repository_id"];
    let maintainer = doc["author_type"] == "User" && policy.maintainer_user_ids.contains(&author);
    let bot = policy.trusted_bots.iter().find(|b| {
        doc["author_type"] == "Bot" && author == b.user_id && doc["author_login"] == b.login
    });
    let classification = if maintainer {
        "configured-maintainer"
    } else if bot.is_some() {
        "configured-automation"
    } else {
        "external"
    };
    let mut latest = HashMap::new();
    for review in reviews {
        let id = review["user_id"].as_u64().unwrap();
        if review["user_type"] != "User"
            || !policy.maintainer_user_ids.contains(&id)
            || !matches!(
                review["state"].as_str(),
                Some("CHANGES_REQUESTED" | "DISMISSED" | "APPROVED")
            )
        {
            continue;
        }
        let order = (
            snapshot::timestamp(&review["submitted_at"])?,
            review["id"].as_u64().unwrap(),
        );
        if latest.get(&id).is_none_or(|(old, _)| old < &order) {
            latest.insert(id, (order, review));
        }
    }
    let blocked = latest
        .values()
        .any(|(_, v)| v["state"] == "CHANGES_REQUESTED");
    let marker = acknowledgment(policy, doc);
    let mut approval = None;
    let mut current = false;
    for (order, r) in latest.values() {
        if r["state"] != "APPROVED" || r["commit_id"] != doc["head_sha"] || r["user_id"] == author {
            continue;
        }
        current = true;
        let body = r["body"].as_str().unwrap();
        if body.matches(MARKER).count() == 1
            && body
                .split([
                    '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}',
                    '\u{2028}', '\u{2029}',
                ])
                .any(|line| line == marker)
            && approval.is_none_or(|(old, _)| old < order)
        {
            approval = Some((order, *r));
        }
    }
    let mut bot_scope = false;
    if let Some(bot) = bot.filter(|_| findings.is_empty()) {
        let allowed = compile(&bot.allowed_patterns, syntax)?;
        bot_scope = true;
        for item in files {
            if !match_any(&allowed, item["filename"].as_str().unwrap(), &mut budget)?
                || !item["previous_filename"].is_null()
            {
                bot_scope = false;
                break;
            }
        }
    }
    let automatic = same && (maintainer || bot_scope);
    let ready = doc["state"] == "open" && doc["draft"] == false;
    let allow = ready && !blocked && (automatic || approval.is_some());
    let reason = if !ready {
        "not-open-ready"
    } else if blocked {
        "maintainer-changes-requested"
    } else if !allow && current {
        "maintainer-acknowledgment-required"
    } else if approval.is_some() {
        "maintainer-review"
    } else if automatic {
        "trusted-source"
    } else {
        "maintainer-review-required"
    };
    let boundary: Map<String, Value> = snapshot::BOUNDARY
        .iter()
        .map(|&k| (k.into(), doc[k].clone()))
        .collect();
    let count = findings.len();
    findings.truncate(256);
    let r = approval.map(|(_, v)| v);
    Ok(
        json!({"schema_version":1,"kind":"pull_request_triage","policy_digest":policy.digest,"boundary":boundary,"classification":classification,
        "provenance":if same{"same-repository"}else{"fork"},"inventory":{"complete":true,"changed_files":files.len(),"content_analysis":false,
            "duplicate_scope":"most-recently-updated-closed-pull-requests","candidate_limit":doc["candidate_limit"],"candidates_examined":doc["candidates_examined"],"merged_candidates":candidates.len()},
        "admission":{"decision":if allow{"allow"}else{"hold"},"reason":reason,"review_id":r.map(|v|&v["id"]),"reviewer_id":r.map(|v|&v["user_id"]),
            "head_sha":doc["head_sha"],"acknowledgment":marker},"finding_count":count,"omitted_findings":count-findings.len(),"findings":findings}),
    )
}
