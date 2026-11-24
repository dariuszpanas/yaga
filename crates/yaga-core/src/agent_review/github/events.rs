//! Parse lifecycle event boundaries before any privileged decision.
use super::{
    PullRequest, bounded, parse_pull_request, positive, record, repository_name, sha, timestamp,
};
use serde_json::Value;
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EventBoundary {
    pub action: String,
    pub disposition: String,
    pub repository: String,
    pub default_branch: String,
    pub pull_request_number: i64,
    pub head_sha: Option<String>,
    pub base_sha: Option<String>,
    pub base_ref: Option<String>,
    pub occurred_at: Option<i64>,
    pub author_id: Option<i64>,
    pub author_login: Option<String>,
}
#[derive(Debug)]
pub struct CodexEvent {
    pub action: String,
    pub disposition: String,
    pub default_branch: String,
    pub pull_request_number: i64,
    pub pull_request: Option<PullRequest>,
    pub occurred_at: Option<i64>,
}
pub fn parse_event_boundary(
    repository: &str,
    event_name: &str,
    event: &Value,
) -> Result<EventBoundary, String> {
    repository_name(repository)?;
    if event_name != "pull_request_target" {
        return Err("Agent review requires a pull_request_target event".into());
    }
    let repo = record(&event["repository"], "GitHub event repository")?;
    if repo["full_name"] != repository {
        return Err("GitHub event belongs to another repository".into());
    }
    let default_branch = bounded(&repo["default_branch"], "default branch", 255)?;
    let action = event["action"]
        .as_str()
        .filter(|s| {
            matches!(
                *s,
                "closed"
                    | "converted_to_draft"
                    | "edited"
                    | "opened"
                    | "ready_for_review"
                    | "reopened"
                    | "synchronize"
            )
        })
        .ok_or("pull request action is invalid")?;
    let pr = record(&event["pull_request"], "GitHub event pull request")?;
    let number = positive(&pr["number"], "pull request number")?;
    if positive(&event["number"], "pull request event number")? != number {
        return Err("pull request event identifies a different pull request".into());
    }
    let state = pr["state"]
        .as_str()
        .filter(|s| matches!(*s, "open" | "closed"))
        .ok_or("pull request event state is invalid")?;
    let draft = pr["draft"]
        .as_bool()
        .ok_or("pull request event state is invalid")?;
    if action == "closed" && state != "closed" {
        return Err("closed pull request event has an invalid state".into());
    }
    let mut result = EventBoundary {
        action: action.into(),
        disposition: "closed".into(),
        repository: repository.into(),
        default_branch,
        pull_request_number: number,
        head_sha: None,
        base_sha: None,
        base_ref: None,
        occurred_at: None,
        author_id: None,
        author_login: None,
    };
    if state == "closed" && matches!(action, "closed" | "edited") {
        return Ok(result);
    }
    if state != "open" {
        return Err("active pull request event is not open".into());
    }
    let head = record(&pr["head"], "pull request event head")?;
    let base = record(&pr["base"], "pull request event base")?;
    let base_repo = record(&base["repo"], "pull request event base repository")?;
    let origin = base_repo["full_name"].as_str().unwrap_or("");
    repository_name(origin)?;
    if origin != repository {
        return Err("pull request event base repository is invalid".into());
    }
    let user = record(&pr["user"], "pull request event author")?;
    result.head_sha = Some(sha(&head["sha"], "pull request event head")?);
    result.base_sha = Some(sha(&base["sha"], "pull request event base")?);
    result.base_ref = Some(bounded(&base["ref"], "pull request event base ref", 255)?);
    result.occurred_at = Some(timestamp(&pr["updated_at"], "pull request event boundary")?);
    result.author_id = Some(positive(&user["id"], "pull request event author ID")?);
    result.author_login = Some(bounded(
        &user["login"],
        "pull request event author login",
        128,
    )?);
    if action == "ready_for_review" && draft {
        return Err("ready transition still identifies a draft pull request".into());
    }
    if action == "converted_to_draft" && !draft {
        return Err("draft transition does not identify a draft pull request".into());
    }
    let base_changed = if action == "edited" && !event["changes"].is_null() {
        let changes = record(&event["changes"], "pull request event changes")?;
        if changes["base"].is_null() {
            false
        } else {
            record(&changes["base"], "pull request base change")?;
            true
        }
    } else {
        false
    };
    result.disposition = if action == "edited" && !base_changed {
        "noop"
    } else if draft {
        "draft"
    } else {
        "review"
    }
    .into();
    Ok(result)
}
pub fn parse_codex_event(
    repository: &str,
    event_name: &str,
    event: &Value,
) -> Result<CodexEvent, String> {
    let b = parse_event_boundary(repository, event_name, event)?;
    let pr = if b.disposition == "closed" {
        None
    } else {
        let pr = parse_pull_request(&event["pull_request"], repository, b.pull_request_number)?;
        if Some(&pr.head_sha) != b.head_sha.as_ref()
            || Some(&pr.base_sha) != b.base_sha.as_ref()
            || Some(&pr.base_ref) != b.base_ref.as_ref()
            || pr.state != "open"
        {
            return Err("full pull request event disagrees with its local boundary".into());
        }
        if b.disposition == "draft" && !pr.draft {
            return Err("draft event does not identify a draft pull request".into());
        }
        if b.disposition == "review" && pr.draft {
            return Err("review event still identifies a draft pull request".into());
        }
        Some(pr)
    };
    Ok(CodexEvent {
        action: b.action,
        disposition: b.disposition,
        default_branch: b.default_branch,
        pull_request_number: b.pull_request_number,
        pull_request: pr,
        occurred_at: b.occurred_at,
    })
}
