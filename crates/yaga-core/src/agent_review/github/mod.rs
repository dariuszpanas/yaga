//! GitHub provider for trusted Agent review operations.
pub mod api;
pub mod boundary;
pub mod candidate;
pub mod events;
pub mod evidence;
pub mod gate;
pub mod publication;
pub mod requests;
pub mod runs;
pub mod runtime;
#[cfg(test)]
mod tests;
use api::RestApi;
use regex::Regex;
use serde_json::Value;
use std::sync::LazyLock;

pub const CODEX_CONNECTOR_USER_ID: i64 = 199_175_422;
pub const CODEX_CONNECTOR_LOGIN: &str = "chatgpt-codex-connector[bot]";
pub const CODEX_CONNECTOR_APP_ID: i64 = 1_144_995;
pub const CODEX_CONNECTOR_APP_SLUG: &str = "chatgpt-codex-connector";
pub const GITHUB_ACTIONS_USER_ID: i64 = 41_898_282;
pub const GITHUB_ACTIONS_LOGIN: &str = "github-actions[bot]";
pub const CODEX_STATUS_CONTEXT: &str = "Agent Review";
pub const CI_STATUS_CONTEXT: &str = "CI Gate";
pub const PENDING_DESCRIPTION: &str = "Waiting for an exact-head Agent review outcome";
pub const TIMEOUT_DESCRIPTION: &str = "Agent review timed out; rerun CI after the agent finishes";
pub const CI_FAILURE_DESCRIPTION: &str = "CI prerequisites failed; Agent review was not requested";

pub fn positive(value: &Value, label: &str) -> Result<i64, String> {
    value
        .as_i64()
        .filter(|n| *n > 0)
        .ok_or_else(|| format!("{label} must be a bounded positive integer"))
}
pub fn sha(value: &Value, label: &str) -> Result<String, String> {
    value
        .as_str()
        .filter(|s| {
            s.len() == 40
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || matches!(b, b'a'..=b'f'))
        })
        .map(str::to_string)
        .ok_or_else(|| format!("{label} must be a full lowercase commit SHA"))
}
pub fn bounded(value: &Value, label: &str, maximum: usize) -> Result<String, String> {
    let s = value
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| format!("{label} must be a bounded string"))?;
    if s.len() > maximum || s.trim() != s || s.bytes().any(|b| b < 32 || b == 127) {
        return Err(format!("{label} contains unsafe or oversized text"));
    }
    Ok(s.into())
}
pub fn timestamp(value: &Value, label: &str) -> Result<i64, String> {
    let s = value
        .as_str()
        .filter(|s| (10..=40).contains(&s.chars().count()))
        .ok_or_else(|| format!("{label} must be a bounded UTC timestamp"))?;
    crate::timestamp::parse_time(&s.replace('Z', "+00:00"))
        .ok_or_else(|| format!("{label} must be an ISO-8601 timestamp"))?
        .ok_or_else(|| format!("{label} must include a timezone"))
}
pub fn repository_name(repo: &str) -> Result<(), String> {
    static NAME: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$").unwrap());
    if !NAME.is_match(repo) {
        return Err("repository must be an owner/name pair".into());
    }
    if repo.split('/').any(|c| matches!(c, "." | "..")) {
        return Err("repository contains an unsafe path segment".into());
    }
    Ok(())
}
pub fn workflow_path(path: &str, label: &str) -> Result<(), String> {
    static WORKFLOW: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^\.github/workflows/[A-Za-z0-9_.-]{1,128}\.ya?ml$").unwrap());
    if !WORKFLOW.is_match(path) {
        return Err(format!("{label} must be a repository workflow path"));
    }
    Ok(())
}
pub fn record<'a>(value: &'a Value, label: &str) -> Result<&'a Value, String> {
    if value.is_object() {
        Ok(value)
    } else {
        Err(format!("{label} must be an object"))
    }
}
pub fn actor_is(value: &Value, id: i64, login: &str) -> bool {
    value["user"]["id"].as_i64() == Some(id) && value["user"]["login"] == login
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PullRequest {
    pub number: i64,
    pub head_sha: String,
    pub head_ref: String,
    pub head_repository: String,
    pub base_ref: String,
    pub base_sha: String,
    pub draft: bool,
    pub state: String,
    pub created_at: i64,
}

pub fn parse_pull_request(
    value: &Value,
    repository: &str,
    expected_number: i64,
) -> Result<PullRequest, String> {
    repository_name(repository)?;
    if expected_number <= 0 {
        return Err("pull request number must be a bounded positive integer".into());
    }
    let value = record(value, "pull request")?;
    let number = positive(&value["number"], "pull request number")?;
    if number != expected_number {
        return Err("GitHub returned a different pull request".into());
    }
    let head = record(&value["head"], "pull request head")?;
    let head_repo = record(&head["repo"], "pull request head repository")?;
    let base = record(&value["base"], "pull request base")?;
    let base_repo = record(&base["repo"], "pull request base repository")?;
    let draft = value["draft"]
        .as_bool()
        .ok_or("pull request draft state is invalid")?;
    let state = value["state"]
        .as_str()
        .filter(|s| matches!(*s, "open" | "closed"))
        .ok_or("pull request state is invalid")?;
    let repo = base_repo["full_name"].as_str().unwrap_or("");
    repository_name(repo)?;
    if repo != repository {
        return Err("pull request base repository does not match the requested repository".into());
    }
    let origin = head_repo["full_name"].as_str().unwrap_or("");
    repository_name(origin)?;
    Ok(PullRequest {
        number,
        head_sha: sha(&head["sha"], "pull request head")?,
        head_ref: bounded(&head["ref"], "pull request head ref", 255)?,
        head_repository: origin.into(),
        base_ref: bounded(&base["ref"], "pull request base ref", 255)?,
        base_sha: sha(&base["sha"], "pull request base")?,
        draft,
        state: state.into(),
        created_at: timestamp(&value["created_at"], "pull request creation")?,
    })
}
pub fn load_pull_request(
    api: &mut dyn RestApi,
    repository: &str,
    number: i64,
) -> Result<PullRequest, String> {
    repository_name(repository)?;
    if number <= 0 {
        return Err("pull request number must be a bounded positive integer".into());
    }
    parse_pull_request(
        &api.get(&format!("/repos/{repository}/pulls/{number}"))?,
        repository,
        number,
    )
}
pub fn load_default_branch(api: &mut dyn RestApi, repository: &str) -> Result<String, String> {
    repository_name(repository)?;
    let v = api.get(&format!("/repos/{repository}"))?;
    record(&v, "repository")?;
    if v["full_name"] != repository {
        return Err("GitHub returned a different repository".into());
    }
    bounded(&v["default_branch"], "repository default branch", 255)
}
