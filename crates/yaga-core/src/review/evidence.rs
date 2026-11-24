use super::{
    Lens,
    markers::{self, State},
    snapshot::*,
};
use crate::agent_review::Outcome;

#[derive(Default)]
pub(super) struct Evidence<'a> {
    pub reviews: Vec<&'a Review>,
    pub inline: Vec<&'a Inline>,
    pub issues: Vec<&'a Issue>,
    pub reactions: Vec<&'a Reaction>,
    pub states: Vec<State>,
    pub requested: bool,
    pub stale: Vec<(Time, u64, String)>,
    pub stale_reactions: bool,
}

pub(super) fn collect<'a>(lens: &Lens<'_>, snapshot: &'a Snapshot) -> Result<Evidence<'a>, String> {
    let mut facts = Evidence::default();
    let mut old_reviews = Vec::new();
    let login = lens.login;
    let head = snapshot.head.as_str();
    let fresh = |time| snapshot.head_time.is_none_or(|threshold| time >= threshold);
    for review in &snapshot.reviews {
        if !lens.matches(&review.actor, "review")?
            || matches!(review.state.as_str(), "PENDING" | "DISMISSED")
        {
            continue;
        }
        let (Some(commit), Some(time)) = (&review.commit, review.time) else {
            return Err(format!(
                "review {} from {login} is missing commit_id or submitted_at",
                review.id
            ));
        };
        if markers::reviewed(&review.body, &format!("review {}", review.id))?
            .is_some_and(|prefix| !commit.starts_with(&prefix))
        {
            return Err(format!(
                "review {} from {login} has inconsistent Reviewed commit marker",
                review.id
            ));
        }
        if commit == head && fresh(time) {
            facts.reviews.push(review)
        } else {
            old_reviews.push(review);
            facts.stale.push((time, review.id, commit.clone()));
        }
    }
    facts.reviews.sort_by_key(|r| (r.time, r.id));
    for comment in &snapshot.inline {
        if !lens.matches(&comment.actor, "review comment")?
            || old_reviews.iter().any(|r| Some(r.id) == comment.review)
        {
            continue;
        }
        let current = match &comment.commit {
            Some(commit) => commit == head && fresh(comment.time),
            None if facts.reviews.iter().any(|r| Some(r.id) == comment.review) => true,
            None => {
                return Err(format!(
                    "review comment {} from {login} has no commit binding",
                    comment.id
                ));
            }
        };
        if current {
            facts.inline.push(comment)
        }
    }
    let mut bound = Vec::new();
    let mut unbound = Vec::new();
    for comment in &snapshot.issues {
        if !lens.matches(&comment.actor, "issue comment")? {
            continue;
        }
        if lens.app.is_some() && lens.app != comment.app {
            return Err(format!(
                "issue comment {} from {login} has an unexpected app_id",
                comment.id
            ));
        }
        if let Some(state) = markers::state(comment, head)? {
            if state.sha == head && fresh(state.time) {
                facts.states.push(state)
            } else {
                facts.stale.push((state.time, state.id, state.sha))
            }
            continue;
        }
        if markers::CLEAN.is_match(&comment.body)
            && comment.updated.is_some_and(|t| t != comment.time)
        {
            return Err(format!(
                "issue comment {} from {login} was edited and cannot be trusted as a clean review outcome",
                comment.id
            ));
        }
        if let Some(prefix) =
            markers::reviewed(&comment.body, &format!("issue comment {}", comment.id))?
        {
            if head.starts_with(&prefix) {
                if old_reviews
                    .iter()
                    .any(|r| r.commit.as_deref().is_some_and(|c| c.starts_with(&prefix)))
                {
                    return Err(format!(
                        "issue comment {} from {login} has an ambiguous Reviewed commit prefix",
                        comment.id
                    ));
                }
                if fresh(comment.time) {
                    bound.push(comment);
                    continue;
                }
            }
            facts.stale.push((comment.time, comment.id, prefix));
        } else {
            unbound.push(comment)
        }
    }
    let stale_time = facts.stale.iter().map(|r| r.0).max();
    for comment in bound {
        if Some(comment.time) > stale_time {
            facts.issues.push(comment)
        } else {
            facts.stale.push((comment.time, comment.id, head.into()))
        }
    }
    for comment in unbound {
        if snapshot.head_time.map_or_else(
            || !markers::CLEAN.is_match(&comment.body),
            |t| comment.time > t && Some(comment.time) > stale_time,
        ) {
            facts.issues.push(comment)
        }
    }
    for reaction in &snapshot.reactions {
        if !lens.matches(&reaction.actor, "reaction")? {
            continue;
        }
        if snapshot.head_time.is_some_and(|t| reaction.time > t) && Some(reaction.time) > stale_time
        {
            facts.reactions.push(reaction)
        } else if lens.pending.contains(&reaction.content.as_str())
            || lens.pass.contains(&reaction.content.as_str())
        {
            facts.stale_reactions = true
        }
    }
    for who in &snapshot.requested {
        facts.requested |= lens.matches(who, "requested reviewer")?
    }
    Ok(facts)
}

pub(super) fn last_state(e: &Evidence<'_>, outcome: Outcome) -> Option<Time> {
    e.states
        .iter()
        .filter(|s| s.outcome == outcome)
        .map(|s| s.time)
        .max()
}

pub(super) struct Runs<'a> {
    pub passed: Vec<&'a Run>,
    pub failed: Vec<&'a Run>,
    pub working: Vec<&'a Run>,
    pub missing: Vec<(&'a str, bool)>,
}
pub(super) fn runs<'a>(lens: &Lens<'a>, snapshot: &'a Snapshot) -> Result<Runs<'a>, String> {
    for run in &snapshot.runs {
        if lens.runs.contains(&run.name.as_str()) && lens.app.is_some() && lens.app != run.app {
            return Err(format!(
                "check-run {} ({}) for {} has an unexpected app_id",
                run.id, run.name, lens.login
            ));
        }
    }
    let mut results = Runs {
        passed: Vec::new(),
        failed: Vec::new(),
        working: Vec::new(),
        missing: Vec::new(),
    };
    for &name in &lens.runs {
        let mut completed = None;
        let mut incomplete = None;
        let mut stale = false;
        for run in snapshot.runs.iter().filter(|r| r.name == name) {
            if run.head != snapshot.head
                || snapshot
                    .head_time
                    .zip(run.end.or(run.start))
                    .is_some_and(|(t, r)| r < t)
            {
                stale = true;
                continue;
            }
            let latest: &mut Option<&Run> = if run.status == "completed" {
                &mut completed
            } else {
                &mut incomplete
            };
            if latest.is_none_or(|prior| match (run.start, prior.start) {
                (Some(a), Some(b)) if a != b => a > b,
                _ => run.id > prior.id,
            }) {
                *latest = Some(run)
            }
        }
        let chosen = match (completed, incomplete) {
            (Some(c), Some(i))
                if i.id > c.id || i.start.zip(c.start.or(c.end)).is_some_and(|(i, c)| i >= c) =>
            {
                Some(i)
            }
            (Some(c), _) => Some(c),
            (None, i) => i,
        };
        match chosen {
            None => results.missing.push((name, stale)),
            Some(r) if r.status != "completed" => results.working.push(r),
            Some(r) if r.conclusion.as_deref() != Some("success") => results.failed.push(r),
            Some(r) => results.passed.push(r),
        }
    }
    Ok(results)
}
