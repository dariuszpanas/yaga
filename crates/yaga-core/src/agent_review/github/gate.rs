//! Trusted lifecycle invalidation, authorization, review settlement and CI publication.
use super::{
    CI_FAILURE_DESCRIPTION, CI_STATUS_CONTEXT, CODEX_STATUS_CONTEXT, TIMEOUT_DESCRIPTION,
    api::RestApi,
    boundary::{ReviewBoundary, parse_boundary_description},
    candidate::{LiveCandidate, load_live_candidate},
    events::{parse_codex_event, parse_event_boundary},
    evidence::{
        CodexOutcome, has_codex_pending_reaction, select_codex_outcome, validate_codex_outcome,
    },
    publication::*,
    requests::{
        RequestComment, RequestKey, ensure_authorization, find_authorization,
        validate_authorization,
    },
    runs::{SourceRun, require_current_source},
    workflow_path,
};
use serde_json::Value;
use std::time::{Duration, Instant};
type Result<T> = std::result::Result<T, String>;
fn wait_until(deadline: Option<Instant>) -> bool {
    let Some(left) = deadline
        .and_then(|d| d.checked_duration_since(Instant::now()))
        .filter(|d| !d.is_zero())
    else {
        return false;
    };
    std::thread::sleep(left.min(Duration::from_secs(30)));
    true
}
const PRIOR: &str =
    "Agent Review blocked because an earlier YAGA request has no trusted success; open a fresh PR";
const UNSOLICITED: &str =
    "Agent Review blocked because connector activity has no exact YAGA request";
const PRECEDENCE: &str =
    "Agent Review blocked because its request does not follow protected approval";
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GateResult {
    pub message: &'static str,
    pub exit_code: u8,
    pub route: &'static str,
}
impl GateResult {
    fn skipped(message: &'static str) -> Self {
        Self {
            message,
            exit_code: 0,
            route: "skip",
        }
    }
    fn route(message: &'static str, route: &'static str) -> Self {
        Self {
            message,
            exit_code: 0,
            route,
        }
    }
    fn failed(message: &'static str) -> Self {
        Self {
            message,
            exit_code: 1,
            route: "skip",
        }
    }
}
pub fn invalidate(
    api: &mut dyn RestApi,
    repo: &str,
    event_name: &str,
    event: &Value,
    run_id: i64,
    run_attempt: i64,
    url: &str,
) -> Result<GateResult> {
    let local = parse_event_boundary(repo, event_name, event)?;
    if matches!(&*local.disposition, "closed" | "noop") {
        return Ok(GateResult::skipped(
            "skipped: event is not a review boundary",
        ));
    }
    let incomplete = "active lifecycle boundary is incomplete";
    let boundary = ReviewBoundary {
        run_id,
        run_attempt,
        action: local.action,
        pull_request_number: local.pull_request_number,
        head_sha: local.head_sha.ok_or(incomplete)?,
        base_sha: local.base_sha.ok_or(incomplete)?,
        occurred_at: local.occurred_at.ok_or(incomplete)?,
    };
    let base_ref = local.base_ref.ok_or(incomplete)?;
    let parsed = parse_codex_event(repo, event_name, event)?;
    let expected = parsed
        .pull_request
        .ok_or("active lifecycle event has no pull request")?;
    let live = load_exact_live(
        api,
        repo,
        &expected,
        &parsed.default_branch,
        parsed.disposition == "review",
    )?;
    if live.is_none_or(|p| p.draft != expected.draft) {
        return Ok(GateResult::skipped(
            "skipped: lifecycle candidate changed before invalidation",
        ));
    }
    if !uniquely_owned(api, repo, &boundary.head_sha, boundary.pull_request_number)? {
        return Err("active lifecycle head is not uniquely owned by this pull request".into());
    }
    ensure_status_capacity(
        api,
        repo,
        &boundary.head_sha,
        &[CODEX_STATUS_CONTEXT, CI_STATUS_CONTEXT],
        8,
    )?;
    let desc = boundary.description("CI pending")?;
    publish_pending(
        api,
        repo,
        &boundary.head_sha,
        url,
        &desc,
        CODEX_STATUS_CONTEXT,
    )?;
    if parsed.default_branch != local.default_branch || parsed.disposition != local.disposition {
        return Err("lifecycle event changed during staged validation".into());
    }
    publish_pending(api, repo, &boundary.head_sha, url, &desc, CI_STATUS_CONTEXT)?;
    Ok(GateResult::skipped(if local.disposition == "draft" {
        "pending: pull request is a draft"
    } else if base_ref != local.default_branch {
        "pending: pull request no longer targets the default branch"
    } else {
        "pending: waiting for exact-head CI prerequisites"
    }))
}
/// Each operation resolves this exact immutable source again after a wait.
pub struct Publisher<'a> {
    pub repository: &'a str,
    pub source: &'a SourceRun,
    pub lifecycle_workflow: &'a str,
    pub server_url: &'a str,
    pub target_url: &'a str,
    pub owner_id: i64,
}
struct Proof<'a> {
    key: &'a RequestKey,
    request: &'a RequestComment,
    approval: Option<&'a RequestComment>,
    outcome: &'a CodexOutcome,
    codex_status: Option<&'a CommitStatus>,
}
fn follows(request: Option<&RequestComment>, approval: Option<&RequestComment>) -> bool {
    approval.is_none_or(|a| {
        a.kind == "approval"
            && request.is_some_and(|r| r.kind == "request" && r.created_at > a.created_at)
    })
}
impl Publisher<'_> {
    fn owner(&self, c: &LiveCandidate) -> Result<bool> {
        if self.owner_id <= 0 {
            return Err("direct review author ID must be a bounded positive integer".into());
        }
        Ok(self.owner_id == c.author_id)
    }
    fn current(&self, api: &mut dyn RestApi) -> Result<Option<LiveCandidate>> {
        require_current_source(api, self.repository, self.source)?;
        load_live_candidate(
            api,
            self.repository,
            self.source,
            self.lifecycle_workflow,
            self.server_url,
        )
    }
    fn revalidate(
        &self,
        api: &mut dyn RestApi,
        c: &LiveCandidate,
        lease: Option<&StatusLease>,
    ) -> Result<bool> {
        let p = &c.pull_request;
        if self.current(api)?.is_none_or(|live| !c.same(&live)) {
            return Ok(false);
        }
        if let Some(l) = lease
            && !lease_is_current(api, self.repository, l)?
        {
            return Ok(false);
        }
        if !uniquely_owned(api, self.repository, &p.head_sha, p.number)? {
            return Ok(false);
        }
        Ok(load_exact_live(api, self.repository, p, &c.default_branch, true)?.is_some())
    }
    fn key(&self, c: &LiveCandidate) -> RequestKey {
        RequestKey {
            repository: self.repository.into(),
            pull_request_number: c.pull_request.number,
            head_sha: c.pull_request.head_sha.clone(),
            base_sha: c.pull_request.base_sha.clone(),
            boundary_run_id: c.boundary.run_id,
            source_run_id: self.source.run_id,
            source_run_attempt: self.source.run_attempt,
        }
    }
    fn pending(
        &self,
        api: &mut dyn RestApi,
        c: &LiveCandidate,
        context: &str,
    ) -> Result<StatusLease> {
        let phase = if context == CI_STATUS_CONTEXT {
            "CI pending"
        } else {
            "Codex pending"
        };
        publish_pending(
            api,
            self.repository,
            &c.pull_request.head_sha,
            self.target_url,
            &c.boundary.description(phase)?,
            context,
        )
    }
    fn current_outcome(
        &self,
        api: &mut dyn RestApi,
        c: &LiveCandidate,
        request: Option<&RequestComment>,
    ) -> Result<Option<CodexOutcome>> {
        let Some(r) = request.filter(|r| r.kind == "request") else {
            return Ok(None);
        };
        let t = c.boundary.occurred_at.max(r.created_at);
        select_codex_outcome(
            api,
            self.repository,
            &c.pull_request,
            t,
            true,
            Some(t + 1_000_000),
        )
    }
    fn unsolicited(&self, api: &mut dyn RestApi, c: &LiveCandidate) -> Result<bool> {
        let t = c.boundary.occurred_at;
        Ok(
            has_codex_pending_reaction(api, self.repository, &c.pull_request, t)?
                || select_codex_outcome(
                    api,
                    self.repository,
                    &c.pull_request,
                    t - 1_000_000,
                    true,
                    Some(t),
                )?
                .is_some(),
        )
    }
    fn prior_settled(&self, api: &mut dyn RestApi, requests: &[RequestComment]) -> Result<bool> {
        for r in requests {
            let k = &r.key;
            let history = status_history(api, self.repository, &k.head_sha, CODEX_STATUS_CONTEXT)?;
            let mut settled = false;
            for w in history.windows(2) {
                let (s, prior) = (&w[0], &w[1]);
                let Some((b, "Codex passed")) = parse_boundary_description(s, &k.head_sha) else {
                    continue;
                };
                if s.state == "success"
                    && s.created_at > r.created_at
                    && s.target_url.is_some()
                    && b.run_id == k.boundary_run_id
                    && b.pull_request_number == k.pull_request_number
                    && b.head_sha == k.head_sha
                    && b.base_sha == k.base_sha
                    && s.trusted(CODEX_STATUS_CONTEXT)
                    && prior.trusted(CODEX_STATUS_CONTEXT)
                    && prior.state == "pending"
                    && prior.target_url == s.target_url
                    && prior.description.as_deref() == Some(&b.description("Codex pending")?)
                {
                    settled = true;
                    break;
                }
            }
            if !settled {
                return Ok(false);
            }
        }
        Ok(true)
    }
    fn codex_lineage(
        &self,
        api: &mut dyn RestApi,
        c: &LiveCandidate,
        s: &CommitStatus,
    ) -> Result<bool> {
        Ok(s.state == "success"
            && terminal_has_pending_lineage(
                api,
                self.repository,
                &c.pull_request.head_sha,
                s,
                &c.boundary.description("Codex pending")?,
            )?
            && parse_boundary_description(s, &c.pull_request.head_sha)
                .is_some_and(|(b, phase)| phase == "Codex passed" && b.same(&c.boundary)))
    }
    fn proof_current(
        &self,
        api: &mut dyn RestApi,
        c: &LiveCandidate,
        proof: Option<&Proof<'_>>,
    ) -> Result<bool> {
        let Some(p) = proof else {
            return Ok(true);
        };
        if let Some(s) = p.codex_status
            && !self.codex_lineage(api, c, s)?
        {
            return Ok(false);
        }
        let t = c.boundary.occurred_at.max(p.request.created_at);
        let request_valid = match validate_authorization(api, p.key, p.request) {
            Ok(valid) => valid,
            Err(err) if err == "GitHub API GET failed with HTTP 404" => false,
            Err(err) => return Err(err),
        };
        if p.request.kind != "request"
            || !validate_codex_outcome(
                api,
                self.repository,
                &c.pull_request,
                t,
                true,
                p.outcome,
                Some(t + 1_000_000),
            )?
            || !request_valid
        {
            return Ok(false);
        }
        if let Some(a) = p.approval
            && !validate_authorization(api, p.key, a)?
        {
            return Ok(false);
        }
        if !follows(Some(p.request), p.approval) {
            return Ok(false);
        }
        let auth = find_authorization(api, p.key)?;
        self.prior_settled(api, &auth.prior_requests)
    }
    /// A successful write always carries a fresh outcome, marker, and exact source.
    /// CI additionally carries the immediately preceding verified Agent Review lease.
    fn settle(
        &self,
        api: &mut dyn RestApi,
        c: &LiveCandidate,
        codex_lease: Option<&StatusLease>,
        proof: Option<&Proof<'_>>,
    ) -> Result<bool> {
        let success = proof.is_some();
        if let Some(p) = proof {
            if codex_lease.is_none() && p.codex_status.is_none() {
                return Err("CI success capabilities are incomplete".into());
            }
            api.require_success_tail(48, 60)?;
        }
        let ci_lease;
        let (lease, phase) = if let Some(l) = codex_lease {
            (
                l,
                if success {
                    "Codex passed"
                } else {
                    "Codex failed"
                },
            )
        } else {
            if !self.revalidate(api, c, None)? {
                return Ok(false);
            }
            ci_lease = self.pending(api, c, CI_STATUS_CONTEXT)?;
            (&ci_lease, if success { "CI passed" } else { "CI failed" })
        };
        if !self.revalidate(api, c, Some(lease))? || !self.proof_current(api, c, proof)? {
            return Ok(false);
        }
        let Some(terminal) = publish_terminal(
            api,
            self.repository,
            lease,
            if success { "success" } else { "error" },
            &c.boundary.description(phase)?,
        )?
        else {
            return Ok(false);
        };
        let checked: Result<bool> = (|| {
            Ok(terminal_is_current(api, self.repository, lease, &terminal)?
                && self.revalidate(api, c, None)?
                && self.proof_current(api, c, proof)?
                && terminal_is_current(api, self.repository, lease, &terminal)?)
        })();
        if !matches!(checked, Ok(true)) {
            restore_pending_after_race(api, self.repository, lease, &terminal);
        }
        checked
    }
    fn fail(
        &self,
        api: &mut dyn RestApi,
        c: &LiveCandidate,
        lease: &StatusLease,
        also_ci: bool,
        message: &'static str,
    ) -> Result<GateResult> {
        self.settle(api, c, Some(lease), None)?;
        if also_ci {
            self.settle(api, c, None, None)?;
        }
        Ok(GateResult::failed(message))
    }
    pub fn prepare(
        &self,
        api: &mut dyn RestApi,
        wake: &str,
        deadline: Option<Instant>,
    ) -> Result<GateResult> {
        self.prepare_with_wait(api, wake, &mut || wait_until(deadline))
    }
    /// The adapter supplies a wait that returns false at the lifecycle deadline.
    pub fn prepare_with_wait(
        &self,
        api: &mut dyn RestApi,
        wake: &str,
        wait: &mut dyn FnMut() -> bool,
    ) -> Result<GateResult> {
        let c = loop {
            match self.current(api) {
                Err(e) => {
                    if !wait() {
                        return Err(e);
                    }
                }
                Ok(None) => {
                    return Ok(GateResult::skipped(
                        "skipped: source candidate is no longer active",
                    ));
                }
                Ok(Some(c)) => break c,
            }
        };
        if !self.revalidate(api, &c, None)? {
            return Ok(GateResult::skipped(
                "skipped: source candidate changed before publication",
            ));
        }
        workflow_path(wake, "publisher wake workflow")?;
        if wake != self.lifecycle_workflow && wake != self.source.workflow_path {
            return Err("publisher wake workflow is not allowed".into());
        }
        let winner = if c.lifecycle_updated_at > self.source.updated_at {
            self.lifecycle_workflow
        } else {
            &self.source.workflow_path
        };
        if wake != winner {
            return Ok(GateResult::skipped(
                "skipped: another completion wake owns this boundary",
            ));
        }
        if self.source.conclusion != "success" {
            let l = self.pending(api, &c, CODEX_STATUS_CONTEXT)?;
            return self.fail(api, &c, &l, true, CI_FAILURE_DESCRIPTION);
        }
        let owner = self.owner(&c)?;
        let l = self.pending(api, &c, CODEX_STATUS_CONTEXT)?;
        self.pending(api, &c, CI_STATUS_CONTEXT)?;
        let key = self.key(&c);
        let a = find_authorization(api, &key)?;
        let request = a.request.as_ref();
        let approval = a.approval.as_ref().filter(|_| !owner);
        if !self.prior_settled(api, &a.prior_requests)? {
            return self.fail(api, &c, &l, true, PRIOR);
        }
        if request.is_none() && self.unsolicited(api, &c)? {
            return self.fail(api, &c, &l, true, UNSOLICITED);
        }
        if request.is_some() && !follows(request, approval) {
            return self.fail(api, &c, &l, true, PRECEDENCE);
        }
        let outcome = self.current_outcome(api, &c, request)?;
        if (owner || approval.is_some())
            && let (Some(outcome), Some(r)) = (outcome.as_ref(), request)
        {
            let proof = Proof {
                outcome,
                request: r,
                approval,
                key: &key,
                codex_status: None,
            };
            return Ok(if self.settle(api, &c, Some(&l), Some(&proof))? {
                GateResult::route("success: Codex already reviewed the exact head", "done")
            } else {
                GateResult::skipped("skipped: candidate changed while publishing success")
            });
        }
        if !owner && approval.is_none() {
            if request.is_some() {
                return self.fail(
                    api,
                    &c,
                    &l,
                    true,
                    "Agent Review blocked because the exact YAGA request lacks protected approval",
                );
            }
            return Ok(GateResult::route(
                "pending: maintainer approval is required before requesting Codex",
                "external",
            ));
        }
        if request.is_some() {
            return Ok(GateResult::route(
                "pending: Agent review is already in flight",
                "observe",
            ));
        }
        Ok(GateResult::route(
            "pending: a bounded Agent review request is needed",
            if owner { "owner" } else { "approved" },
        ))
    }
    pub fn authorize(&self, api: &mut dyn RestApi) -> Result<GateResult> {
        if self.source.conclusion != "success" {
            return Ok(GateResult::skipped("skipped: source CI did not succeed"));
        }
        let Some(c) = self.current(api)? else {
            return Ok(GateResult::skipped(
                "skipped: source candidate is no longer active",
            ));
        };
        if self.owner(&c)? {
            return Err("owner-authored pull requests do not use external approval".into());
        }
        if !self.revalidate(api, &c, None)? {
            return Ok(GateResult::skipped(
                "skipped: source candidate changed before approval",
            ));
        }
        ensure_authorization(api, &self.key(&c), "approval", &mut |api| {
            self.revalidate(api, &c, None)
        })?;
        Ok(GateResult::route(
            "success: protected maintainer approval recorded",
            "done",
        ))
    }
    pub fn review(
        &self,
        api: &mut dyn RestApi,
        allow_request: bool,
        deadline: Instant,
    ) -> Result<GateResult> {
        self.review_with_wait(api, allow_request, &mut || wait_until(Some(deadline)))
    }
    /// The adapter supplies a wait that returns false when the polling window closes.
    pub fn review_with_wait(
        &self,
        api: &mut dyn RestApi,
        allow_request: bool,
        wait: &mut dyn FnMut() -> bool,
    ) -> Result<GateResult> {
        if self.source.conclusion != "success" {
            return Ok(GateResult::skipped("skipped: source CI did not succeed"));
        }
        let Some(c) = self.current(api)? else {
            return Ok(GateResult::skipped(
                "skipped: source candidate is no longer active",
            ));
        };
        if !self.revalidate(api, &c, None)? {
            return Ok(GateResult::skipped(
                "skipped: source candidate changed before publication",
            ));
        }
        let lease = self.pending(api, &c, CODEX_STATUS_CONTEXT)?;
        let key = self.key(&c);
        let a = find_authorization(api, &key)?;
        let owner = self.owner(&c)?;
        let approval = a.approval.as_ref().filter(|_| !owner);
        let mut request = a.request;
        let prior_ok = self.prior_settled(api, &a.prior_requests)?;
        if !owner && approval.is_none() {
            return Err("external Agent review request lacks protected approval".into());
        }
        if !prior_ok {
            return self.fail(api, &c, &lease, false, PRIOR);
        }
        if request.is_none() && self.unsolicited(api, &c)? {
            return self.fail(api, &c, &lease, false, UNSOLICITED);
        }
        if !allow_request && request.is_none() {
            return self.fail(
                api,
                &c,
                &lease,
                false,
                "Agent Review blocked because its exact YAGA request is missing",
            );
        }
        if request.is_none() {
            if !self.revalidate(api, &c, Some(&lease))? {
                return Ok(GateResult::skipped(
                    "skipped: candidate changed before Codex authorization",
                ));
            }
            enum Race {
                Request,
                Unsolicited,
            }
            let mut race = None;
            let result = ensure_authorization(api, &key, "request", &mut |api| {
                if !self.revalidate(api, &c, Some(&lease))? {
                    return Ok(false);
                }
                let latest = find_authorization(api, &key)?;
                if !self.prior_settled(api, &latest.prior_requests)? {
                    return Ok(false);
                }
                if !owner {
                    let Some(approval) = latest.approval else {
                        return Ok(false);
                    };
                    if !validate_authorization(api, &key, &approval)? {
                        return Ok(false);
                    }
                }
                if latest.request.is_some() {
                    race = Some(Race::Request);
                    return Ok(false);
                }
                if self.unsolicited(api, &c)? {
                    race = Some(Race::Unsolicited);
                    return Ok(false);
                }
                Ok(true)
            });
            request = match race {
                Some(Race::Request) => find_authorization(api, &key)?.request,
                Some(Race::Unsolicited) => return self.fail(
                    api,
                    &c,
                    &lease,
                    false,
                    "Agent Review blocked because connector activity appeared before its request",
                ),
                None => Some(result?),
            };
        }
        let Some(r) = request.as_ref() else {
            return self.fail(
                api,
                &c,
                &lease,
                false,
                "Agent Review blocked because its exact YAGA request disappeared",
            );
        };
        if !follows(Some(r), approval) {
            return self.fail(api, &c, &lease, false, PRECEDENCE);
        }
        loop {
            if api.require_success_tail(80, 60).is_err() {
                api.require_success_tail(32, 60)?;
                return self.fail(api, &c, &lease, false, TIMEOUT_DESCRIPTION);
            }
            if !self.revalidate(api, &c, Some(&lease))? {
                return Ok(GateResult::skipped(
                    "skipped: candidate changed while waiting for Codex",
                ));
            }
            if let Some(outcome) = self.current_outcome(api, &c, Some(r))? {
                let proof = Proof {
                    key: &key,
                    request: r,
                    approval,
                    outcome: &outcome,
                    codex_status: None,
                };
                return Ok(if self.settle(api, &c, Some(&lease), Some(&proof))? {
                    GateResult::route(
                        "success: trusted exact-head Agent review is current",
                        "done",
                    )
                } else {
                    GateResult::skipped("skipped: candidate changed while publishing success")
                });
            }
            if !wait() {
                return self.fail(api, &c, &lease, false, TIMEOUT_DESCRIPTION);
            }
        }
    }
    pub fn finalize(&self, api: &mut dyn RestApi) -> Result<GateResult> {
        let Some(c) = self.current(api)? else {
            return Ok(GateResult::skipped(
                "skipped: source candidate is no longer active",
            ));
        };
        let head = &c.pull_request.head_sha;
        let codex = latest_trusted_status(api, self.repository, head, CODEX_STATUS_CONTEXT)?;
        let claim = codex
            .as_ref()
            .and_then(|s| parse_boundary_description(s, head));
        let lineage = if let Some(s) = &codex {
            terminal_has_pending_lineage(
                api,
                self.repository,
                head,
                s,
                &c.boundary.description("Codex pending")?,
            )?
        } else {
            false
        };
        let owner = self.owner(&c)?;
        let key = self.key(&c);
        let a = find_authorization(api, &key)?;
        let approval = a.approval.as_ref().filter(|_| !owner);
        let request = a.request.as_ref();
        let prior_ok = self.prior_settled(api, &a.prior_requests)?;
        let eligible = self.source.conclusion == "success"
            && codex.as_ref().is_some_and(|s| s.state == "success")
            && request.is_some()
            && follows(request, approval)
            && prior_ok
            && matches!(claim,Some((b,"Codex passed")) if b.same(&c.boundary))
            && lineage
            && (owner || approval.is_some());
        let outcome = if eligible {
            self.current_outcome(api, &c, request)?
        } else {
            None
        };
        let proof = outcome.as_ref().zip(request).map(|(outcome, r)| Proof {
            key: &key,
            request: r,
            approval,
            outcome,
            codex_status: codex.as_ref(),
        });
        let settled = self.settle(api, &c, None, proof.as_ref())?;
        Ok(if !settled {
            GateResult::skipped("skipped: candidate changed during CI Gate publication")
        } else if proof.is_some() {
            GateResult::route("success: CI prerequisites and Agent review passed", "done")
        } else {
            GateResult::failed("CI Gate failed because Agent Review is not successful")
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::agent_review::github::{
        CODEX_CONNECTOR_APP_ID, CODEX_CONNECTOR_APP_SLUG, CODEX_CONNECTOR_LOGIN,
        CODEX_CONNECTOR_USER_ID, GITHUB_ACTIONS_LOGIN, GITHUB_ACTIONS_USER_ID, PullRequest,
    };
    use serde_json::json;

    struct FakeApi {
        comment_error: &'static str,
    }
    impl RestApi for FakeApi {
        fn get(&mut self, path: &str) -> Result<Value> {
            if path.ends_with("/issues/comments/10") {
                return Ok(json!({
                    "id": 10,
                    "user": {"id": CODEX_CONNECTOR_USER_ID, "login": CODEX_CONNECTOR_LOGIN},
                    "performed_via_github_app": {"id": CODEX_CONNECTOR_APP_ID, "slug": CODEX_CONNECTOR_APP_SLUG},
                    "created_at": "2026-11-01T12:01:00Z",
                    "updated_at": "2026-11-01T12:01:00Z",
                    "body": format!("Agent Review: Didn't find any major issues.\n\n**Reviewed commit:** `{}`", "a".repeat(40)),
                }));
            }
            if path.ends_with("/issues/comments/20") {
                return Err(self.comment_error.into());
            }
            Err(format!("unexpected path: {path}"))
        }
        fn post(&mut self, _: &str, _: &Value) -> Result<Value> {
            Err("unexpected post".into())
        }
        fn require_success_tail(&self, _: usize, _: u64) -> Result<()> {
            Ok(())
        }
    }

    #[test]
    fn proof_current_treats_deleted_request_404_as_false_and_propagates_5xx() {
        let head = "a".repeat(40);
        let base = "b".repeat(40);
        let source = SourceRun {
            run_id: 12,
            run_attempt: 1,
            run_number: 1,
            workflow_path: ".github/workflows/ci.yml".into(),
            head_sha: head.clone(),
            conclusion: "success".into(),
            created_at: 1_793_534_400_000_000,
            updated_at: 1_793_534_400_000_000,
            pull_request_number: 9,
            pull_request_action: "opened".into(),
            base_sha: base.clone(),
            head_ref: "feat/x".into(),
            head_repository: "ex/project".into(),
        };
        let publisher = Publisher {
            repository: "ex/project",
            source: &source,
            lifecycle_workflow: ".github/workflows/review-policy.yml",
            server_url: "https://github.com",
            target_url: "https://github.com/ex/project/actions/runs/12/attempts/1",
            owner_id: 1,
        };
        let boundary = ReviewBoundary {
            run_id: 8,
            run_attempt: 1,
            action: "opened".into(),
            pull_request_number: 9,
            head_sha: head.clone(),
            base_sha: base.clone(),
            occurred_at: 1_793_534_400_000_000,
        };
        let candidate = LiveCandidate {
            pull_request: PullRequest {
                number: 9,
                head_sha: head.clone(),
                head_ref: "feat/x".into(),
                head_repository: "ex/project".into(),
                base_ref: "main".into(),
                base_sha: base.clone(),
                draft: false,
                state: "open".into(),
                created_at: 1_793_534_400_000_000,
            },
            author_id: 1,
            author_login: "owner".into(),
            default_branch: "main".into(),
            boundary,
            lifecycle_updated_at: 1_793_534_400_000_000,
            latest_codex_status: CommitStatus {
                status_id: 1,
                state: "pending".into(),
                context: CODEX_STATUS_CONTEXT.into(),
                description: None,
                target_url: None,
                created_at: 1_793_534_400_000_000,
                creator_id: GITHUB_ACTIONS_USER_ID,
                creator_login: GITHUB_ACTIONS_LOGIN.into(),
            },
        };
        let key = publisher.key(&candidate);
        let request = RequestComment {
            comment_id: 20,
            key: key.clone(),
            kind: "request".into(),
            created_at: 1_793_534_400_000_000,
            updated_at: 1_793_534_400_000_000,
        };
        let outcome = CodexOutcome {
            occurred_at: 1_793_534_460_000_000,
            database_id: 10,
            kind: "clean comment".into(),
            reviewed_commit: head,
        };
        let proof = Proof {
            key: &key,
            request: &request,
            approval: None,
            outcome: &outcome,
            codex_status: None,
        };

        let mut api_404 = FakeApi {
            comment_error: "GitHub API GET failed with HTTP 404",
        };
        assert!(
            !publisher
                .proof_current(&mut api_404, &candidate, Some(&proof))
                .unwrap()
        );

        let mut api_500 = FakeApi {
            comment_error: "GitHub API GET failed with HTTP 500",
        };
        assert_eq!(
            publisher
                .proof_current(&mut api_500, &candidate, Some(&proof))
                .unwrap_err(),
            "GitHub API GET failed with HTTP 500"
        );
    }
}
