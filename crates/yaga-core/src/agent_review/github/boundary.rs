//! Authenticate the lifecycle transition encoded in Actions-owned status descriptions.
use super::{
    CODEX_STATUS_CONTEXT,
    api::RestApi,
    publication::CommitStatus,
    repository_name,
    runs::{belongs, listed, listing, position},
    sha, timestamp, workflow_path,
};
use chrono::{DateTime, SecondsFormat};
use regex::Regex;
use serde_json::json;
use std::{collections::HashSet, net::Ipv6Addr, sync::LazyLock};

const ACTIONS: [(&str, &str); 7] = [
    ("closed", "c"),
    ("converted_to_draft", "d"),
    ("edited", "e"),
    ("opened", "o"),
    ("ready_for_review", "y"),
    ("reopened", "r"),
    ("synchronize", "s"),
];
const PHASES: [(&str, &str); 6] = [
    ("CI pending", "CP"),
    ("Codex pending", "RP"),
    ("Codex passed", "RS"),
    ("Codex failed", "RF"),
    ("CI passed", "CS"),
    ("CI failed", "CF"),
];
static DESCRIPTION: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\AYAGA boundary ([1-9][0-9]{0,18})/([1-9][0-9]{0,3}) ([cdeorys]) PR ([1-9][0-9]{0,18}) base ([0-9a-f]{40}) at ([^\r\n]{10,40}) (CP|RP|RS|RF|CS|CF)\z").unwrap()
});
static TITLE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\AYAGA (?:(closed|converted_to_draft|edited|opened|ready_for_review|reopened|synchronize) boundary|metadata edit) for #([1-9][0-9]{0,18})\z").unwrap()
});
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReviewBoundary {
    pub run_id: i64,
    pub run_attempt: i64,
    pub action: String,
    pub pull_request_number: i64,
    pub head_sha: String,
    pub base_sha: String,
    pub occurred_at: i64,
}
impl ReviewBoundary {
    pub fn same(&self, b: &Self) -> bool {
        self.run_id == b.run_id
            && self.run_attempt == b.run_attempt
            && self.action == b.action
            && self.pull_request_number == b.pull_request_number
            && self.head_sha == b.head_sha
            && self.base_sha == b.base_sha
    }
    pub fn description(&self, phase: &str) -> Result<String, String> {
        let p = PHASES
            .iter()
            .find(|(v, _)| *v == phase)
            .ok_or("YAGA boundary phase is invalid")?
            .1;
        let a = ACTIONS
            .iter()
            .find(|(v, _)| *v == self.action)
            .ok_or("YAGA boundary phase is invalid")?
            .1;
        if self.run_attempt <= 0 || self.run_id <= 0 || self.pull_request_number <= 0 {
            return Err("boundary requires positive run, attempt and PR identities".into());
        }
        if self.run_attempt > 9999 {
            return Err("YAGA boundary attempt is too large".into());
        }
        sha(&json!(self.base_sha), "boundary base")?;
        let occurred = DateTime::from_timestamp_micros(self.occurred_at)
            .ok_or("invalid boundary occurrence")?
            .to_rfc3339_opts(SecondsFormat::Secs, true);
        let s = format!(
            "YAGA boundary {}/{} {a} PR {} base {} at {occurred} {p}",
            self.run_id, self.run_attempt, self.pull_request_number, self.base_sha
        );
        if s.len() > 140 {
            return Err("YAGA boundary description is too long".into());
        }
        Ok(s)
    }
}
pub fn parse_boundary_description(
    status: &CommitStatus,
    head: &str,
) -> Option<(ReviewBoundary, &'static str)> {
    let m = DESCRIPTION.captures(status.description.as_deref()?)?;
    let b = ReviewBoundary {
        run_id: m[1].parse().ok()?,
        run_attempt: m[2].parse().ok()?,
        action: ACTIONS.iter().find(|(_, v)| *v == &m[3])?.0.into(),
        pull_request_number: m[4].parse().ok()?,
        base_sha: m[5].into(),
        head_sha: sha(&json!(head), "boundary head").ok()?,
        occurred_at: timestamp(&json!(&m[6]), "boundary occurrence").ok()?,
    };
    Some((b, PHASES.iter().find(|(_, v)| *v == &m[7])?.0))
}
pub(super) fn server_origin(s: &str) -> Result<&str, String> {
    let fail = || "GitHub server URL is invalid".to_owned();
    let origin = s.strip_suffix('/').unwrap_or(s);
    let authority = origin.strip_prefix("https://").ok_or_else(fail)?;
    if s.len() > 2048
        || authority.is_empty()
        || authority
            .bytes()
            .any(|b| b <= 32 || b >= 127 || b"/\\@?#".contains(&b))
    {
        return Err(fail());
    }
    let port = if let Some(ip) = authority.strip_prefix('[') {
        let (ip, tail) = ip.split_once(']').ok_or_else(fail)?;
        ip.parse::<Ipv6Addr>().map_err(|_| fail())?;
        if tail.is_empty() {
            None
        } else {
            Some(tail.strip_prefix(':').ok_or_else(fail)?)
        }
    } else {
        let (host, port) = authority
            .split_once(':')
            .map_or((authority, None), |(h, p)| (h, Some(p)));
        if host.is_empty()
            || !host
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".-_".contains(&b))
        {
            return Err(fail());
        }
        port
    };
    if port.is_some_and(|p| {
        p.is_empty()
            || !p.bytes().all(|b| b.is_ascii_digit())
            || !matches!(p.parse::<u16>(), Ok(1..))
    }) {
        return Err(fail());
    }
    Ok(origin)
}
pub fn validate_boundary_source(
    api: &mut dyn RestApi,
    repo: &str,
    path: &str,
    server: &str,
    b: &ReviewBoundary,
    status: &CommitStatus,
) -> Result<i64, String> {
    repository_name(repo)?;
    workflow_path(path, "lifecycle workflow")?;
    let url = format!(
        "{}/{repo}/actions/runs/{}/attempts/{}",
        server_origin(server)?,
        b.run_id,
        b.run_attempt
    );
    if !status.trusted(CODEX_STATUS_CONTEXT)
        || status.state != "pending"
        || status.target_url.as_ref() != Some(&url)
    {
        return Err("lifecycle boundary status provenance is invalid".into());
    }
    if parse_boundary_description(status, &b.head_sha)
        .as_ref()
        .is_none_or(|(got, p)| got != b || *p != "CI pending")
    {
        return Err("lifecycle boundary status does not carry the expected boundary".into());
    }
    let run = api.get(&format!("/repos/{repo}/actions/runs/{}", b.run_id))?;
    if run["id"].as_i64() != Some(b.run_id)
        || run["run_attempt"].as_i64() != Some(b.run_attempt)
        || !belongs(&run, repo, path, "pull_request_target")
        || run["status"] != "completed"
        || run["conclusion"] != "success"
        || run["head_sha"] != b.head_sha
        || run["display_title"]
            != format!("YAGA {} boundary for #{}", b.action, b.pull_request_number)
    {
        return Err("lifecycle boundary workflow run is not trusted".into());
    }
    let created = timestamp(&run["created_at"], "lifecycle workflow creation")?;
    let updated = timestamp(&run["updated_at"], "lifecycle workflow update")?;
    if updated < created || b.occurred_at > created || status.created_at < created {
        return Err("lifecycle boundary timing is invalid".into());
    }
    let current = position(&run, "lifecycle workflow")?;
    let response = listing(api, repo, path, &b.head_sha, "pull_request_target")?;
    let mut seen = HashSet::new();
    let mut current_seen = false;
    for (i, r) in listed(&response, "lifecycle workflow run")?
        .iter()
        .enumerate()
    {
        let pos = position(r, &format!("lifecycle workflow run {i}"))?;
        if !seen.insert(pos.2) {
            return Err("lifecycle workflow run ID is repeated".into());
        }
        if !belongs(r, repo, path, "pull_request_target") || r["head_sha"] != b.head_sha {
            return Err("lifecycle workflow run list contains an untrusted run".into());
        }
        let title = r["display_title"]
            .as_str()
            .and_then(|s| TITLE.captures(s))
            .filter(|m| m[2].parse::<i64>().ok() == Some(b.pull_request_number))
            .ok_or("lifecycle workflow run title is invalid")?;
        current_seen |= pos == current;
        if pos > current && title.get(1).is_some() {
            return Err("a newer lifecycle boundary exists for this head".into());
        }
    }
    if !current_seen {
        return Err("lifecycle boundary is outside the bounded run window".into());
    }
    Ok(updated)
}
