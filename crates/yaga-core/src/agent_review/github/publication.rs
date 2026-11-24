//! Acquire attempt-owned pending leases, validate physical status lineage, and repair uncertain writes.
#[cfg(test)]
#[path = "publication_tests.rs"]
mod tests;
use super::{
    GITHUB_ACTIONS_LOGIN, GITHUB_ACTIONS_USER_ID, PullRequest, api::RestApi, bounded,
    load_pull_request, positive, repository_name, sha, timestamp,
};
use crate::text::casefold;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommitStatus {
    pub status_id: i64,
    pub state: String,
    pub context: String,
    pub description: Option<String>,
    pub target_url: Option<String>,
    pub created_at: i64,
    pub creator_id: i64,
    pub creator_login: String,
}
fn nullable(v: &Value, label: &str, max: usize) -> Result<Option<String>, String> {
    if v.is_null() {
        return Ok(None);
    }
    if v.as_str() == Some("") {
        return Ok(Some(String::new()));
    }
    bounded(v, label, max).map(Some)
}
pub fn parse_status(v: &Value, label: &str) -> Result<CommitStatus, String> {
    let state = v["state"]
        .as_str()
        .filter(|s| matches!(*s, "error" | "failure" | "pending" | "success"))
        .ok_or_else(|| format!("{label} state is invalid"))?;
    Ok(CommitStatus {
        status_id: positive(&v["id"], &format!("{label} ID"))?,
        state: state.into(),
        context: bounded(&v["context"], &format!("{label} context"), 100)?,
        description: nullable(&v["description"], &format!("{label} description"), 140)?,
        target_url: nullable(&v["target_url"], &format!("{label} target URL"), 2048)?,
        created_at: timestamp(&v["created_at"], &format!("{label} creation"))?,
        creator_id: positive(&v["creator"]["id"], &format!("{label} creator ID"))?,
        creator_login: bounded(
            &v["creator"]["login"],
            &format!("{label} creator login"),
            128,
        )?,
    })
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StatusLease {
    pub status_id: i64,
    pub head_sha: String,
    pub target_url: String,
    pub context: String,
    pub description: String,
}
impl CommitStatus {
    pub fn trusted(&self, context: &str) -> bool {
        self.context == context
            && self.creator_id == GITHUB_ACTIONS_USER_ID
            && self.creator_login == GITHUB_ACTIONS_LOGIN
    }
    fn matches_lease(&self, l: &StatusLease) -> bool {
        self.trusted(&l.context)
            && self.status_id == l.status_id
            && self.state == "pending"
            && self.target_url.as_ref() == Some(&l.target_url)
            && self.description.as_ref() == Some(&l.description)
    }
    fn matches_terminal(&self, l: &StatusLease, terminal: &Self) -> bool {
        matches!(&*terminal.state, "error" | "success")
            && self.trusted(&l.context)
            && self.status_id == terminal.status_id
            && self.state == terminal.state
            && self.target_url.as_ref() == Some(&l.target_url)
    }
}
fn publish(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    state: &str,
    description: &str,
    url: &str,
    context: &str,
) -> Result<CommitStatus, String> {
    repository_name(repo)?;
    sha(&json!(head), "Agent review status head")?;
    if description.is_empty()
        || description.chars().count() > 140
        || description.bytes().any(|b| b < 32 || b == 127)
    {
        return Err("Agent review status description is invalid".into());
    }
    let response = api.post(
        &format!("/repos/{repo}/statuses/{head}"),
        &json!({"context":context,"description":description,"state":state,"target_url":url}),
    )?;
    let s = parse_status(&response, "published Agent review status")?;
    if s.creator_id != GITHUB_ACTIONS_USER_ID || s.creator_login != GITHUB_ACTIONS_LOGIN {
        return Err("published Agent review status has an unexpected creator".into());
    }
    if !s.trusted(context)
        || s.state != state
        || s.description.as_deref() != Some(description)
        || s.target_url.as_deref() != Some(url)
    {
        return Err("GitHub returned a different Agent review status".into());
    }
    Ok(s)
}
pub fn publish_pending(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    url: &str,
    description: &str,
    context: &str,
) -> Result<StatusLease, String> {
    let s = publish(api, repo, head, "pending", description, url, context)?;
    Ok(StatusLease {
        status_id: s.status_id,
        head_sha: head.into(),
        target_url: url.into(),
        description: description.into(),
        context: context.into(),
    })
}
pub fn repair_pending(api: &mut dyn RestApi, repo: &str, l: &StatusLease) -> bool {
    publish_pending(
        api,
        repo,
        &l.head_sha,
        &l.target_url,
        &l.description,
        &l.context,
    )
    .is_ok()
}
fn page(api: &mut dyn RestApi, repo: &str, head: &str, n: usize) -> Result<Value, String> {
    repository_name(repo)?;
    sha(&json!(head), "Agent review status head")?;
    api.get(&format!(
        "/repos/{repo}/commits/{head}/statuses?per_page=100&page={n}"
    ))
}
#[derive(Default)]
struct Sequence {
    ids: HashSet<i64>,
    previous: Option<i64>,
}
impl Sequence {
    fn check(&mut self, status: &CommitStatus, label: &str) -> Result<(), String> {
        if !self.ids.insert(status.status_id) {
            return Err(format!("{label} repeats a status"));
        }
        if self.previous.is_some_and(|t| status.created_at > t) {
            return Err(format!("{label} is not newest-first"));
        }
        self.previous = Some(status.created_at);
        Ok(())
    }
}
fn history(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    context: &str,
) -> Result<(Vec<CommitStatus>, usize), String> {
    let v = page(api, repo, head, 1)?;
    let items = v
        .as_array()
        .filter(|v| v.len() < 100)
        .ok_or("Agent review status page is incomplete or invalid")?;
    let mut order = Sequence::default();
    let mut statuses = Vec::new();
    let context = casefold(context);
    for (i, v) in items.iter().enumerate() {
        let s = parse_status(v, &format!("Agent review status {i}"))?;
        order.check(&s, "Agent review status page")?;
        if casefold(&s.context) == context {
            statuses.push(s);
        }
    }
    Ok((statuses, items.len()))
}
pub fn status_history(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    ctx: &str,
) -> Result<Vec<CommitStatus>, String> {
    history(api, repo, head, ctx).map(|v| v.0)
}
pub fn ensure_status_capacity(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    contexts: &[&str],
    reserve: usize,
) -> Result<(), String> {
    if contexts.is_empty()
        || contexts.iter().any(|s| s.is_empty())
        || contexts.iter().collect::<HashSet<_>>().len() != contexts.len()
    {
        return Err("Agent review status capacity contexts are invalid".into());
    }
    if !(1..1000).contains(&reserve) {
        return Err("Agent review status capacity reserve is invalid".into());
    }
    let mut counts: HashMap<_, usize> = contexts.iter().map(|s| (casefold(s), 0)).collect();
    let mut order = Sequence::default();
    for n in 1..=10 {
        let v = page(api, repo, head, n)?;
        let items = v
            .as_array()
            .filter(|a| a.len() <= 100)
            .ok_or("Agent review status capacity page is invalid")?;
        for (i, v) in items.iter().enumerate() {
            let s = parse_status(
                v,
                &format!("Agent review status capacity page {n} item {i}"),
            )?;
            order.check(&s, "Agent review status capacity")?;
            if let Some(c) = counts.get_mut(&casefold(&s.context)) {
                *c += 1;
            }
        }
        if items.len() < 100 {
            if counts.values().any(|c| c + reserve > 1000) {
                return Err(
                    "commit status history lacks capacity for a complete YAGA boundary".into(),
                );
            }
            return Ok(());
        }
    }
    Err("commit status history reached GitHub's per-SHA scan ceiling".into())
}
pub fn latest_trusted_status(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    ctx: &str,
) -> Result<Option<CommitStatus>, String> {
    let first = status_history(api, repo, head, ctx)?.into_iter().next();
    if first.as_ref().is_some_and(|s| !s.trusted(ctx)) {
        return Err(format!("latest {ctx} status is not trusted"));
    }
    Ok(first)
}
pub fn lease_is_current(
    api: &mut dyn RestApi,
    repo: &str,
    l: &StatusLease,
) -> Result<bool, String> {
    Ok(status_history(api, repo, &l.head_sha, &l.context)?
        .first()
        .is_some_and(|s| s.matches_lease(l)))
}
pub fn publish_terminal(
    api: &mut dyn RestApi,
    repo: &str,
    l: &StatusLease,
    state: &str,
    description: &str,
) -> Result<Option<CommitStatus>, String> {
    if !matches!(state, "error" | "success") {
        return Err("Agent review terminal state is invalid".into());
    }
    let (statuses, n) = history(api, repo, &l.head_sha, &l.context).inspect_err(|_| {
        repair_pending(api, repo, l);
    })?;
    let first = statuses.first();
    if first.is_none_or(|s| !s.matches_lease(l)) {
        if first.is_none_or(|s| !s.trusted(&l.context) || s.status_id == l.status_id) {
            repair_pending(api, repo, l);
        }
        return Ok(None);
    }
    if n > 98 {
        return Err("commit status page lacks a terminal and repair slot".into());
    }
    publish(
        api,
        repo,
        &l.head_sha,
        state,
        description,
        &l.target_url,
        &l.context,
    )
    .map(Some)
    .inspect_err(|_| {
        repair_pending(api, repo, l);
    })
}
pub fn terminal_is_current(
    api: &mut dyn RestApi,
    repo: &str,
    l: &StatusLease,
    t: &CommitStatus,
) -> Result<bool, String> {
    Ok(
        matches!(status_history(api,repo,&l.head_sha,&l.context)?.as_slice(),[first,prior,..] if first.matches_terminal(l,t)&&prior.matches_lease(l)),
    )
}
pub fn terminal_has_pending_lineage(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    t: &CommitStatus,
    description: &str,
) -> Result<bool, String> {
    Ok(
        matches!(status_history(api,repo,head,&t.context)?.as_slice(),[first,prior,..] if first==t && matches!(&*t.state,"error"|"success")&&t.trusted(&t.context)&&t.target_url.is_some()&&prior.trusted(&t.context)&&prior.state=="pending"&&prior.target_url==t.target_url&&prior.description.as_deref()==Some(description)),
    )
}
pub fn restore_pending_after_race(
    api: &mut dyn RestApi,
    repo: &str,
    l: &StatusLease,
    t: &CommitStatus,
) -> bool {
    if let Ok(history) = status_history(api, repo, &l.head_sha, &l.context)
        && let Some(first) = history.first()
        && first.trusted(&l.context)
    {
        if first.status_id != t.status_id {
            return false;
        }
        if first.matches_terminal(l, t)
            && let Some(prior) = history.get(1)
            && prior.trusted(&l.context)
            && prior.state == "pending"
            && prior.status_id != l.status_id
            && let (Some(url), Some(desc)) = (&prior.target_url, &prior.description)
        {
            return publish_pending(
                api,
                repo,
                &l.head_sha,
                if url.is_empty() { &l.target_url } else { url },
                if desc.is_empty() {
                    &l.description
                } else {
                    desc
                },
                &l.context,
            )
            .is_ok();
        }
    }
    repair_pending(api, repo, l)
}
pub fn load_exact_live(
    api: &mut dyn RestApi,
    repo: &str,
    p: &PullRequest,
    default: &str,
    ready: bool,
) -> Result<Option<PullRequest>, String> {
    let live = load_pull_request(api, repo, p.number)?;
    Ok((p.number == live.number
        && p.head_sha == live.head_sha
        && p.head_ref == live.head_ref
        && p.head_repository == live.head_repository
        && p.base_sha == live.base_sha
        && p.base_ref == live.base_ref
        && live.state == "open"
        && live.base_ref == default
        && (!ready || !live.draft))
        .then_some(live))
}
pub(super) fn associated(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
) -> Result<Option<Vec<i64>>, String> {
    repository_name(repo)?;
    sha(&json!(head), "Agent review head ownership")?;
    let v = api.get(&format!(
        "/repos/{repo}/commits/{head}/pulls?per_page=100&page=1"
    ))?;
    let a = v
        .as_array()
        .filter(|a| a.len() <= 100)
        .ok_or("Agent review head associations are invalid")?;
    if a.len() == 100 {
        return Ok(None);
    }
    let mut open = Vec::new();
    let mut seen = HashSet::new();
    for i in a {
        let state = i["state"]
            .as_str()
            .filter(|s| matches!(*s, "open" | "closed"))
            .ok_or("Agent review head association state is invalid")?;
        let n = positive(&i["number"], "associated pull request number")?;
        if !seen.insert(n) {
            return Err("Agent review head association is repeated".into());
        }
        let oid = sha(&i["head"]["sha"], "associated pull request head SHA")?;
        if state == "open" && oid == head {
            open.push(n);
        }
    }
    Ok(Some(open))
}
pub fn uniquely_owned(
    api: &mut dyn RestApi,
    repo: &str,
    head: &str,
    number: i64,
) -> Result<bool, String> {
    Ok(associated(api, repo, head)?.is_some_and(|a| a == [number]))
}
