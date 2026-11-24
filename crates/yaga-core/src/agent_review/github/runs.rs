//! Authenticate paired CI and lifecycle workflow runs for one immutable head.
use super::{api::RestApi, bounded, positive, repository_name, sha, timestamp, workflow_path};
use regex::Regex;
use serde_json::{Value, json};
use std::{collections::HashSet, sync::LazyLock};

static SOURCE_TITLE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\AYAGA CI (opened|ready_for_review|reopened|synchronize) for #([1-9][0-9]{0,18}) at base ([0-9a-f]{40})\z").unwrap()
});
static LIFECYCLE_TITLE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\AYAGA (?:(converted_to_draft|edited|opened|ready_for_review|reopened|synchronize) boundary|metadata edit) for #([1-9][0-9]{0,18})\z").unwrap()
});
#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SourceRun {
    pub run_id: i64,
    pub run_attempt: i64,
    pub run_number: i64,
    pub workflow_path: String,
    pub head_sha: String,
    pub conclusion: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub pull_request_number: i64,
    pub pull_request_action: String,
    pub base_sha: String,
    pub head_ref: String,
    pub head_repository: String,
}
#[derive(Debug, Eq, PartialEq)]
struct Lifecycle {
    position: Position,
    head: String,
    conclusion: String,
    action: Option<String>,
    number: i64,
    created: i64,
    updated: i64,
}
pub(super) type Position = (i64, i64, i64);
pub(super) fn position(v: &Value, label: &str) -> Result<Position, String> {
    Ok((
        positive(&v["run_number"], &format!("{label} number"))?,
        positive(&v["run_attempt"], &format!("{label} attempt"))?,
        positive(&v["id"], &format!("{label} ID"))?,
    ))
}
pub(super) fn listed<'a>(response: &'a Value, label: &str) -> Result<&'a [Value], String> {
    let runs = response["workflow_runs"]
        .as_array()
        .filter(|r| r.len() < 100 && response["total_count"].as_u64() == Some(r.len() as u64))
        .ok_or_else(|| format!("{label} list is incomplete or invalid"))?;
    Ok(runs)
}
pub(super) fn listing(
    api: &mut dyn RestApi,
    repo: &str,
    path: &str,
    head: &str,
    event: &str,
) -> Result<Value, String> {
    repository_name(repo)?;
    workflow_path(path, "workflow")?;
    sha(&json!(head), "workflow head")?;
    let filename = path.rsplit('/').next().ok_or("workflow path is invalid")?;
    api.get(&format!("/repos/{repo}/actions/workflows/{filename}/runs?event={event}&head_sha={head}&page=1&per_page=100"))
}
pub(super) fn belongs(v: &Value, repo: &str, path: &str, event: &str) -> bool {
    v["repository"]["full_name"] == repo && v["path"] == path && v["event"] == event
}
fn completed<'a>(
    v: &'a Value,
    repo: &str,
    path: &str,
    event: &str,
    label: &str,
) -> Result<(&'a str, i64, i64), String> {
    let outcome = v["conclusion"]
        .as_str()
        .filter(|s| {
            matches!(
                *s,
                "action_required"
                    | "cancelled"
                    | "failure"
                    | "neutral"
                    | "skipped"
                    | "stale"
                    | "success"
                    | "timed_out"
            )
        })
        .ok_or_else(|| format!("{label} conclusion is invalid"))?;
    if !belongs(v, repo, path, event) || v["status"] != "completed" {
        return Err(format!(
            "{label} is not a completed {}",
            if event == "pull_request" {
                "pull-request prerequisite run"
            } else {
                "lifecycle run"
            }
        ));
    }
    let created = timestamp(&v["created_at"], &format!("{label} creation"))?;
    let updated = timestamp(&v["updated_at"], &format!("{label} update"))?;
    if updated < created {
        return Err(format!("{label} update predates its creation"));
    }
    Ok((outcome, created, updated))
}
fn source(v: &Value, repo: &str, path: &str, label: &str) -> Result<SourceRun, String> {
    let (conclusion, created_at, updated_at) = completed(v, repo, path, "pull_request", label)?;
    let title = v["display_title"]
        .as_str()
        .and_then(|s| SOURCE_TITLE.captures(s))
        .ok_or_else(|| format!("{label} title is not a bounded YAGA CI boundary"))?;
    let pull_request_number = title[2]
        .parse::<i64>()
        .map_err(|_| format!("{label} title PR must be a bounded positive integer"))?;
    let (run_number, run_attempt, run_id) = position(v, label)?;
    let head_repository = v["head_repository"]["full_name"]
        .as_str()
        .ok_or("source head repository is invalid")?
        .to_owned();
    repository_name(&head_repository)?;
    Ok(SourceRun {
        run_id,
        run_number,
        run_attempt,
        workflow_path: path.into(),
        head_sha: sha(&v["head_sha"], &format!("{label} head"))?,
        conclusion: conclusion.into(),
        created_at,
        updated_at,
        pull_request_number,
        pull_request_action: title[1].into(),
        base_sha: title[3].into(),
        head_ref: bounded(&v["head_branch"], &format!("{label} head branch"), 255)?,
        head_repository,
    })
}
fn lifecycle(v: &Value, repo: &str, path: &str, label: &str) -> Result<Lifecycle, String> {
    let (outcome, created, updated) = completed(v, repo, path, "pull_request_target", label)?;
    let m = v["display_title"]
        .as_str()
        .and_then(|s| LIFECYCLE_TITLE.captures(s))
        .ok_or_else(|| format!("{label} title is not a bounded lifecycle wake"))?;
    Ok(Lifecycle {
        position: position(v, label)?,
        head: sha(&v["head_sha"], &format!("{label} head"))?,
        conclusion: outcome.into(),
        action: m.get(1).map(|v| v.as_str().into()),
        number: m[2]
            .parse()
            .map_err(|_| format!("{label} title PR must be a bounded positive integer"))?,
        created,
        updated,
    })
}
pub fn require_current_source(
    api: &mut dyn RestApi,
    repo: &str,
    s: &SourceRun,
) -> Result<(), String> {
    repository_name(repo)?;
    let current = source(
        &api.get(&format!("/repos/{repo}/actions/runs/{}", s.run_id))?,
        repo,
        &s.workflow_path,
        "source CI run",
    )?;
    if current != *s {
        return Err("source CI run or attempt changed".into());
    }
    let response = listing(api, repo, &s.workflow_path, &s.head_sha, "pull_request")?;
    let mut ids = HashSet::new();
    let mut newer = false;
    for (i, v) in listed(&response, "source CI workflow run")?
        .iter()
        .enumerate()
    {
        let run = source(
            v,
            repo,
            &s.workflow_path,
            &format!("source CI workflow run {i}"),
        )?;
        if run.head_sha != s.head_sha {
            return Err("source CI workflow run list contains a different head".into());
        }
        if !ids.insert(run.run_id) {
            return Err("source CI workflow run ID is repeated".into());
        }
        newer |=
            (run.run_number, run.run_attempt, run.run_id) > (s.run_number, s.run_attempt, s.run_id);
    }
    if !ids.contains(&s.run_id) || newer {
        return Err("a newer source CI run exists for this head".into());
    }
    Ok(())
}
pub fn load_source_from_wake(
    api: &mut dyn RestApi,
    repo: &str,
    event_name: &str,
    event: &Value,
    ci: &str,
    lifecycle_workflow: &str,
) -> Result<Option<SourceRun>, String> {
    repository_name(repo)?;
    workflow_path(ci, "prerequisite workflow")?;
    workflow_path(lifecycle_workflow, "lifecycle workflow")?;
    if event_name != "workflow_run" || event["action"] != "completed" {
        return Err("Codex publisher requires a completed workflow_run event".into());
    }
    if event["repository"]["full_name"] != repo {
        return Err("workflow event belongs to another repository".into());
    }
    let v = &event["workflow_run"];
    if v["path"] == ci {
        let event_run = source(v, repo, ci, "workflow event source CI run")?;
        let direct = source(
            &api.get(&format!("/repos/{repo}/actions/runs/{}", event_run.run_id))?,
            repo,
            ci,
            "source CI run",
        )?;
        if direct != event_run {
            return Err("workflow event source CI run changed".into());
        }
        require_current_source(api, repo, &direct)?;
        return Ok(Some(direct));
    }
    if v["path"] != lifecycle_workflow {
        return Err("workflow event is not an allowed YAGA wake".into());
    }
    let wake = lifecycle(v, repo, lifecycle_workflow, "workflow event lifecycle run")?;
    let current = lifecycle(
        &api.get(&format!("/repos/{repo}/actions/runs/{}", wake.position.2))?,
        repo,
        lifecycle_workflow,
        "current lifecycle run",
    )?;
    if current != wake {
        return Err("workflow event lifecycle run changed".into());
    }
    if wake.conclusion != "success" || wake.action.is_none() {
        return Ok(None);
    }
    let response = listing(api, repo, ci, &wake.head, "pull_request")?;
    let mut latest = None;
    let mut ids = HashSet::new();
    for (i, v) in listed(&response, "paired CI workflow run")?
        .iter()
        .enumerate()
    {
        let pos = position(v, &format!("paired CI workflow run {i}"))?;
        if !ids.insert(pos.2) {
            return Err("paired CI workflow run ID is repeated".into());
        }
        if !belongs(v, repo, ci, "pull_request") || v["head_sha"] != wake.head {
            return Err("paired CI workflow run list contains an untrusted run".into());
        }
        if latest.is_none_or(|(p, _)| pos > p) {
            latest = Some((pos, v));
        }
    }
    let Some((_, v)) = latest else {
        return Ok(None);
    };
    if v["status"] != "completed" {
        return Ok(None);
    }
    let s = source(v, repo, ci, "paired source CI run")?;
    if s.pull_request_number != wake.number || Some(&s.pull_request_action) != wake.action.as_ref()
    {
        return Ok(None);
    }
    require_current_source(api, repo, &s)?;
    Ok(Some(s))
}
