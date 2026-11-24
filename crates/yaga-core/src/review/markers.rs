//! Strict interpretation of provider-authored, head-bound summary markers.
use super::snapshot::{Issue, Time, hex};
use crate::{agent_review::Outcome, json::strict_value};
use regex::Regex;
use std::sync::LazyLock;

macro_rules! pattern {
    ($name:ident,$value:literal) => {
        pub(super) static $name: LazyLock<Regex> = LazyLock::new(|| Regex::new($value).unwrap());
    };
}
pattern!(BADGE, r"(?i)!\[P[0-3]\s+Badge\]");
pattern!(
    CHANGES,
    r"(?im)^###[ \t]+(?:\S+[ \t]+)?Changes[ \t]+recommended[ \t]*\r?$"
);
pattern!(
    APPROVAL,
    r"(?ims)<!--\s*ccr-overview-v\d+\s*-->.*?^###[ \t]+(?:\S+[ \t]+)?Approval[ \t]+recommended[ \t]*\r?$"
);
pattern!(
    UNABLE,
    r"(?i)\bcopilot\s+(?:encountered\s+an\s+error\s+and\s+)?was\s+unable\s+to\s+review\s+this\s+pull\s+request\b"
);
pattern!(
    CLEAN,
    r"(?i)(?:agent|codex)\s+review:\s*didn['’]t\s+find\s+any\s+major\s+issues\."
);
pattern!(
    REVIEWED,
    r"(?im)^\*\*Reviewed commit:\*\*\s*`([0-9a-f]{7,40})`\s*$"
);
pattern!(STATE, r"(?s)<!--\s*codex-review-state:v1\s+(\{.*?\})\s*-->");

pub(super) fn findings(body: &str) -> bool {
    BADGE.is_match(body) || CHANGES.is_match(body)
}
pub(super) fn reviewed(body: &str, label: &str) -> Result<Option<String>, String> {
    let body = body.replace("\r\n", "\n").replace('\r', "\n");
    let mut all = REVIEWED.captures_iter(&body);
    let first = all.next().map(|m| m[1].to_ascii_lowercase());
    if all.next().is_some() {
        return Err(format!("{label} contains multiple Reviewed commit markers"));
    }
    Ok(first)
}
#[derive(Debug)]
pub(super) struct State {
    pub sha: String,
    pub outcome: Outcome,
    pub time: Time,
    pub id: u64,
}
pub(super) fn state(comment: &Issue, head: &str) -> Result<Option<State>, String> {
    let mut markers = STATE.captures_iter(&comment.body);
    let Some(m) = markers.next() else {
        return Ok(None);
    };
    let label = format!("issue comment {}", comment.id);
    if markers.next().is_some() {
        return Err(format!(
            "{label} contains multiple codex-review-state markers"
        ));
    }
    let payload = strict_value(m[1].as_bytes())
        .map_err(|_| format!("{label} has invalid codex-review-state JSON"))?;
    if !payload.is_object() {
        return Err(format!("{label} codex-review-state must be an object"));
    }
    let mut active = Vec::new();
    for name in ["code_review", "security_review"] {
        let section = &payload[name];
        if section.is_null() {
            continue;
        }
        let detail = format!("{label} {name}");
        if !section.is_object() {
            return Err(format!("{detail} state must be an object"));
        }
        let flag = |key: &str| {
            if section[key].is_null() {
                Ok(None)
            } else {
                section[key]
                    .as_bool()
                    .map(Some)
                    .ok_or_else(|| format!("{detail}.{key} must be a boolean or null"))
            }
        };
        let any = flag("has_any_findings")?;
        let unresolved = flag("has_unresolved_findings")?;
        if section["commit_sha"].is_null() && section["status"].is_null() {
            if any.is_some() || unresolved.is_some() {
                return Err(format!("{detail} inactive state cannot set finding flags"));
            }
            continue;
        }
        let sha = section["commit_sha"]
            .as_str()
            .filter(|s| hex(s, 40))
            .ok_or_else(|| format!("{detail}.commit_sha is invalid"))?;
        let outcome = match section["status"].as_str() {
            Some("completed") => match (any, unresolved) {
                (Some(false), Some(true)) => {
                    return Err(format!("{detail} finding flags are contradictory"));
                }
                (_, Some(true)) | (Some(true), None) => Outcome::Failed,
                (Some(false), _) | (_, Some(false)) => Outcome::Passed,
                _ if name == "security_review" => Outcome::Passed,
                _ => return Err(format!("{detail} finding flags are invalid")),
            },
            Some("running" | "in_progress" | "queued" | "pending") => Outcome::Pending,
            _ => return Err(format!("{detail}.status is invalid")),
        };
        active.push((sha, outcome));
    }
    let Some(&(first, _)) = active.first() else {
        return Err(format!(
            "{label} codex-review-state has no active review section"
        ));
    };
    let current = active.iter().any(|(s, _)| *s == head);
    let outcomes: Vec<_> = active
        .iter()
        .filter(|(s, _)| !current || *s == head)
        .map(|(_, o)| *o)
        .collect();
    let outcome = if outcomes.contains(&Outcome::Failed) {
        Outcome::Failed
    } else if outcomes.contains(&Outcome::Pending) {
        Outcome::Pending
    } else {
        Outcome::Passed
    };
    Ok(Some(State {
        sha: if current { head } else { first }.into(),
        outcome,
        time: comment.updated.unwrap_or(comment.time).max(comment.time),
        id: comment.id,
    }))
}
