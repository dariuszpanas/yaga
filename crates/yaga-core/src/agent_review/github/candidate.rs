//! Resolve the sole ready default-branch PR and its authenticated lifecycle boundary.
use super::{
    CODEX_STATUS_CONTEXT, PullRequest,
    api::RestApi,
    boundary::{ReviewBoundary, parse_boundary_description, validate_boundary_source},
    bounded, load_default_branch, parse_pull_request, positive,
    publication::{CommitStatus, associated, status_history},
    repository_name,
    runs::SourceRun,
};
#[derive(Clone, Debug)]
pub struct LiveCandidate {
    pub pull_request: PullRequest,
    pub author_id: i64,
    pub author_login: String,
    pub default_branch: String,
    pub boundary: ReviewBoundary,
    pub lifecycle_updated_at: i64,
    pub latest_codex_status: CommitStatus,
}
impl LiveCandidate {
    pub fn same(&self, other: &Self) -> bool {
        self.pull_request == other.pull_request
            && self.author_id == other.author_id
            && self.author_login == other.author_login
            && self.default_branch == other.default_branch
            && self.boundary.same(&other.boundary)
            && self.lifecycle_updated_at == other.lifecycle_updated_at
    }
}
pub fn load_live_candidate(
    api: &mut dyn RestApi,
    repo: &str,
    source: &SourceRun,
    workflow: &str,
    server: &str,
) -> Result<Option<LiveCandidate>, String> {
    repository_name(repo)?;
    let open = associated(api, repo, &source.head_sha)?
        .ok_or("source head association list is incomplete")?;
    let number = match open.as_slice() {
        [] => return Ok(None),
        [n] => *n,
        _ => return Err("source head has ambiguous open pull-request ownership".into()),
    };
    if number != source.pull_request_number {
        return Err("source CI belongs to a different pull request".into());
    }
    let v = api.get(&format!("/repos/{repo}/pulls/{number}"))?;
    let p = parse_pull_request(&v, repo, number)?;
    let author_id = positive(&v["user"]["id"], "live pull request author ID")?;
    let author_login = bounded(&v["user"]["login"], "live pull request author login", 128)?;
    let default_branch = load_default_branch(api, repo)?;
    if p.state != "open"
        || p.draft
        || p.head_sha != source.head_sha
        || p.base_ref != default_branch
        || p.base_sha != source.base_sha
        || p.head_ref != source.head_ref
        || p.head_repository != source.head_repository
    {
        return Ok(None);
    }
    let statuses = status_history(api, repo, &p.head_sha, CODEX_STATUS_CONTEXT)?;
    let latest = statuses
        .first()
        .ok_or("Agent Review has no lifecycle boundary")?;
    if !latest.trusted(CODEX_STATUS_CONTEXT) {
        return Err("latest Agent Review status is not Actions-owned".into());
    }
    let (claim, phase) = parse_boundary_description(latest, &p.head_sha)
        .ok_or("latest Agent Review status has no valid lifecycle boundary")?;
    if !matches!(
        (latest.state.as_str(), phase),
        ("pending", "CI pending" | "Codex pending")
            | ("success", "Codex passed")
            | ("error", "Codex failed")
    ) {
        return Err("latest Agent Review status has no valid lifecycle boundary".into());
    }
    if claim.pull_request_number != p.number
        || claim.base_sha != p.base_sha
        || matches!(&*claim.action, "closed" | "converted_to_draft")
    {
        return Err("Agent Review lifecycle boundary is not the live candidate".into());
    }
    let (boundary, provenance) = statuses
        .iter()
        .find_map(|s| {
            let (b, phase) = parse_boundary_description(s, &p.head_sha)?;
            (phase == "CI pending" && b.same(&claim)).then_some((b, s))
        })
        .ok_or("Agent Review lifecycle provenance is outside the bounded history")?;
    let lifecycle_updated_at =
        validate_boundary_source(api, repo, workflow, server, &boundary, provenance)?;
    if source.pull_request_action != boundary.action || source.created_at < boundary.occurred_at {
        return Err("source CI was not created for the current lifecycle boundary".into());
    }
    Ok(Some(LiveCandidate {
        pull_request: p,
        author_id,
        author_login,
        default_branch,
        boundary,
        lifecycle_updated_at,
        latest_codex_status: latest.clone(),
    }))
}
