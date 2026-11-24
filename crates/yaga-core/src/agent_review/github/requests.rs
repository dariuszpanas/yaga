//! Current-boundary Actions-owned request and protected approval markers.
use super::{
    GITHUB_ACTIONS_LOGIN, GITHUB_ACTIONS_USER_ID, actor_is, api::RestApi, positive, record,
    repository_name, sha, timestamp,
};
use regex::Regex;
use serde_json::{Value, json};
use std::{collections::HashSet, sync::LazyLock};
pub const REQUEST_LITERAL: &str = "@codex review";
pub const APPROVAL_LITERAL: &str = "YAGA recorded maintainer approval for this exact Codex result.";
const AUTHORIZATION_MARKER: &str = "yaga-codex-authorization:v2";

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RequestKey {
    pub repository: String,
    pub pull_request_number: i64,
    pub head_sha: String,
    pub base_sha: String,
    pub boundary_run_id: i64,
    pub source_run_id: i64,
    pub source_run_attempt: i64,
}
impl RequestKey {
    fn validate(&self) -> Result<(), String> {
        repository_name(&self.repository)?;
        for (value, name) in [
            (&self.head_sha, "Codex request head"),
            (&self.base_sha, "Codex request base"),
        ] {
            sha(&json!(value), name)?;
        }
        for (value, label) in [
            (self.pull_request_number, "Codex request pull request"),
            (self.boundary_run_id, "Codex request boundary run"),
            (self.source_run_id, "Codex request source run"),
            (self.source_run_attempt, "Codex request source run attempt"),
        ] {
            positive(&value.into(), label)?;
        }
        Ok(())
    }
    fn same_boundary(&self, other: &Self) -> bool {
        self.repository == other.repository
            && self.pull_request_number == other.pull_request_number
            && self.head_sha == other.head_sha
            && self.base_sha == other.base_sha
            && self.boundary_run_id == other.boundary_run_id
    }
    pub fn body_for(&self, kind: &str) -> Result<String, String> {
        self.validate()?;
        let literal = match kind {
            "request" => REQUEST_LITERAL,
            "approval" => APPROVAL_LITERAL,
            _ => return Err("Codex authorization kind is invalid".into()),
        };
        Ok(format!(
            "{literal}\n\n<!-- {AUTHORIZATION_MARKER} kind={kind} repository={} pull-request={} head-sha={} base-sha={} boundary-run-id={} source-run-id={} source-run-attempt={} -->",
            self.repository,
            self.pull_request_number,
            self.head_sha,
            self.base_sha,
            self.boundary_run_id,
            self.source_run_id,
            self.source_run_attempt
        ))
    }
    fn path(&self) -> String {
        format!(
            "/repos/{}/issues/{}/comments",
            self.repository, self.pull_request_number
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestComment {
    pub comment_id: i64,
    pub key: RequestKey,
    pub kind: String,
    pub created_at: i64,
    pub updated_at: i64,
}
#[derive(Default, Debug)]
pub struct AuthorizationComments {
    pub request: Option<RequestComment>,
    pub approval: Option<RequestComment>,
    pub prior_requests: Vec<RequestComment>,
}
impl AuthorizationComments {
    pub fn any(&self) -> Option<&RequestComment> {
        self.request.as_ref().or(self.approval.as_ref())
    }
    fn matching(self, kind: &str) -> Option<RequestComment> {
        if kind == "request" {
            self.request
        } else {
            self.approval
        }
    }
}
fn parse_body(s: &str) -> Option<(RequestKey, String)> {
    if s.len() > 1024 {
        return None;
    }
    static BODY: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\A(@codex review|YAGA recorded maintainer approval for this exact Codex result\.)\n\n<!-- yaga-codex-authorization:v2 kind=(approval|request) repository=([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}) pull-request=([1-9][0-9]{0,18}) head-sha=([0-9a-f]{40}) base-sha=([0-9a-f]{40}) boundary-run-id=([1-9][0-9]{0,18}) source-run-id=([1-9][0-9]{0,18}) source-run-attempt=([1-9][0-9]{0,18}) -->\z").unwrap()
    });
    let g = BODY.captures(s)?;
    let key = RequestKey {
        repository: g[3].into(),
        pull_request_number: g[4].parse().ok()?,
        head_sha: g[5].into(),
        base_sha: g[6].into(),
        boundary_run_id: g[7].parse().ok()?,
        source_run_id: g[8].parse().ok()?,
        source_run_attempt: g[9].parse().ok()?,
    };
    if key.body_for(&g[2]).ok()? != s {
        return None;
    }
    Some((key, g[2].into()))
}
fn parse_comment(v: &Value, label: &str) -> Result<(i64, Option<RequestComment>), String> {
    record(v, label)?;
    let id = positive(&v["id"], &format!("{label} ID"))?;
    let app = &v["performed_via_github_app"];
    if !actor_is(v, GITHUB_ACTIONS_USER_ID, GITHUB_ACTIONS_LOGIN)
        || app["id"].as_i64() != Some(15368)
        || app["slug"] != "github-actions"
    {
        return Ok((id, None));
    }
    let s = v["body"].as_str().unwrap_or("");
    let Some((key, kind)) = parse_body(s) else {
        if s.starts_with(REQUEST_LITERAL)
            || s.starts_with(APPROVAL_LITERAL)
            || s.contains(AUTHORIZATION_MARKER)
        {
            return Err(format!("{label} has a malformed YAGA Codex authorization"));
        }
        return Ok((id, None));
    };
    let created_at = timestamp(&v["created_at"], &format!("{label} creation"))?;
    let updated_at = timestamp(&v["updated_at"], &format!("{label} update"))?;
    if updated_at < created_at {
        return Err("Codex authorization comment update predates its creation".into());
    }
    Ok((
        id,
        Some(RequestComment {
            comment_id: id,
            key,
            kind,
            created_at,
            updated_at,
        }),
    ))
}
pub fn find_authorization(
    api: &mut dyn RestApi,
    key: &RequestKey,
) -> Result<AuthorizationComments, String> {
    key.validate()?;
    let comments = api.paginate(&key.path())?;
    if comments.len() >= 100 {
        return Err("Codex authorization comment history is truncated or invalid".into());
    }
    let mut seen = HashSet::new();
    let mut auth = AuthorizationComments::default();
    for (index, v) in comments.iter().enumerate() {
        let (id, comment) = parse_comment(v, &format!("Codex authorization comment {index}"))?;
        if !seen.insert(id) {
            return Err("Codex authorization comment history repeats a comment".into());
        }
        let Some(c) = comment else {
            continue;
        };
        if !c.key.same_boundary(key) {
            if c.kind == "request"
                && c.key.repository == key.repository
                && c.key.pull_request_number == key.pull_request_number
            {
                if auth
                    .prior_requests
                    .iter()
                    .any(|old| old.key.same_boundary(&c.key))
                {
                    return Err("multiple exact prior Codex request markers exist".into());
                }
                auth.prior_requests.push(c);
                if auth.prior_requests.len() > 8 {
                    return Err("Codex prior request history exceeds its bounded limit".into());
                }
            }
            continue;
        }
        let slot = if c.kind == "request" {
            &mut auth.request
        } else {
            &mut auth.approval
        };
        if slot.is_some() {
            return Err(format!(
                "multiple exact Codex {} markers exist for this boundary",
                c.kind
            ));
        }
        *slot = Some(c);
    }
    Ok(auth)
}
pub fn ensure_authorization(
    api: &mut dyn RestApi,
    key: &RequestKey,
    kind: &str,
    before_post: &mut dyn FnMut(&mut dyn RestApi) -> Result<bool, String>,
) -> Result<RequestComment, String> {
    let body = key.body_for(kind)?;
    for _ in 0..2 {
        if let Some(c) = find_authorization(api, key)?.matching(kind) {
            return Ok(c);
        }
    }
    if !before_post(api)? {
        return Err("Codex authorization candidate changed immediately before publication".into());
    }
    let response = api.post(&key.path(), &json!({"body":body})).and_then(|v| {
        let (_, c) = parse_comment(&v, "published Codex authorization comment")?;
        c.filter(|c| c.key == *key && c.kind == kind)
            .ok_or_else(|| "GitHub returned a different Codex authorization comment".to_owned())
    });
    if let Some(confirmed) = find_authorization(api, key)?.matching(kind) {
        if response.is_ok_and(|c| c.comment_id != confirmed.comment_id) {
            return Err("GitHub returned an inconsistent Codex authorization comment".into());
        }
        return Ok(confirmed);
    }
    Err(response.err().unwrap_or_else(|| {
        "published Codex authorization comment is not visible after creation".into()
    }))
}
pub fn validate_authorization(
    api: &mut dyn RestApi,
    key: &RequestKey,
    comment: &RequestComment,
) -> Result<bool, String> {
    key.validate()?;
    positive(&comment.comment_id.into(), "Codex authorization comment ID")?;
    let v = api.get(&format!(
        "/repos/{}/issues/comments/{}",
        key.repository, comment.comment_id
    ))?;
    Ok(parse_comment(&v, "current Codex authorization comment")?
        .1
        .as_ref()
        == Some(comment)
        && comment.key.same_boundary(key))
}
