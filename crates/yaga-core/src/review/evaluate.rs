use super::{Lens, evidence, markers, snapshot::Snapshot};
use crate::agent_review::{
    LensResult,
    Outcome::{self, Failed, Passed, Pending},
};
use std::collections::HashSet;

pub(super) fn lens(lens: &Lens<'_>, snapshot: &Snapshot) -> Result<LensResult, String> {
    let facts = evidence::collect(lens, snapshot)?;
    let runs = evidence::runs(lens, snapshot)?;
    let head = &snapshot.head[..12];
    let login = lens.login;
    let result = |outcome: Outcome, summary: String| {
        Ok(LensResult {
            lens: lens.name.into(),
            outcome,
            summary: Some(summary),
        })
    };
    if facts.requested {
        return result(
            Pending,
            format!("{login} review is in progress (requested) for head {head}"),
        );
    }

    let latest_review_state = |state: &str| {
        facts
            .reviews
            .iter()
            .filter(|r| r.state == state)
            .filter_map(|r| r.time)
            .max()
    };
    let approval = latest_review_state("APPROVED");
    let changes = latest_review_state("CHANGES_REQUESTED");
    let active_approval = approval > changes;
    let passed = evidence::last_state(&facts, Passed);
    let failed = evidence::last_state(&facts, Failed);
    let pending = evidence::last_state(&facts, Pending);
    let resolved = passed > failed && passed > changes;
    let inline_ids: HashSet<_> = facts.inline.iter().filter_map(|c| c.review).collect();
    let commented: Vec<_> = facts
        .reviews
        .iter()
        .copied()
        .filter(|r| r.state == "COMMENTED")
        .collect();
    let badge = commented
        .iter()
        .filter(|r| markers::findings(&r.body))
        .filter_map(|r| r.time)
        .max();
    let clean = |r: &&super::snapshot::Review| {
        !inline_ids.contains(&r.id)
            && !markers::UNABLE.is_match(&r.body)
            && !markers::findings(&r.body)
    };
    let overview = commented
        .iter()
        .copied()
        .filter(clean)
        .filter(|r| markers::APPROVAL.is_match(&r.body))
        .filter_map(|r| r.time)
        .max();
    let active_overview = overview > changes && overview > badge;
    let cleared = |time| {
        (active_approval && approval > time)
            || (active_overview && overview > time)
            || (resolved && passed >= time)
    };
    let inline = facts
        .inline
        .iter()
        .map(|c| {
            let parent = facts
                .reviews
                .iter()
                .find(|r| Some(r.id) == c.review)
                .and_then(|r| r.time);
            c.time.max(parent.unwrap_or(c.time))
        })
        .max();
    if inline.is_some() && !cleared(inline) {
        return result(
            Failed,
            format!(
                "{login} reported {} inline finding(s) on head {head}",
                facts.inline.len()
            ),
        );
    }
    if changes.is_some() && !active_approval {
        return result(Failed, format!("{login} requested changes on head {head}"));
    }
    if badge.is_some() && !cleared(badge) {
        return result(
            Failed,
            format!("{login} reported review findings on head {head}"),
        );
    }
    if failed.is_some() && !(active_approval && approval > failed) && !resolved {
        return result(
            Failed,
            format!("{login} reported unresolved findings on head {head}"),
        );
    }
    if facts
        .issues
        .iter()
        .any(|c| !markers::CLEAN.is_match(&c.body))
    {
        return result(
            Failed,
            format!("{login} posted a non-clean issue comment on head {head}"),
        );
    }
    if let Some(run) = runs.failed.first() {
        return result(
            Failed,
            format!(
                "check-run {} for {login} completed with {} on head {head}",
                run.name,
                run.conclusion.as_deref().unwrap()
            ),
        );
    }
    if let Some(run) = runs.working.first() {
        return result(
            Pending,
            format!(
                "check-run {} for {login} is {} for head {head}",
                run.name, run.status
            ),
        );
    }
    if let Some((name, stale)) = runs.missing.first() {
        let message = if *stale {
            format!("last check-run {name} for {login} is stale; waiting for head {head}")
        } else {
            format!("waiting for check-run {name} for {login} on head {head}")
        };
        return result(Pending, message);
    }
    let unable = commented
        .iter()
        .filter(|r| markers::UNABLE.is_match(&r.body))
        .filter_map(|r| r.time)
        .max();
    let review = facts
        .reviews
        .iter()
        .rev()
        .copied()
        .find(|r| r.state == "APPROVED" || (r.state == "COMMENTED" && clean(r)));
    let issue = facts.issues.iter().copied().max_by_key(|c| (c.time, c.id));
    let reaction = |choices: &[&str]| {
        facts
            .reactions
            .iter()
            .copied()
            .filter(|r| choices.contains(&r.content.as_str()))
            .max_by_key(|r| (r.time, r.id))
    };
    let pass_reaction = reaction(&lens.pass);
    let pending_reaction = reaction(&lens.pending);
    let completion = [
        review.and_then(|r| r.time),
        passed,
        issue.map(|c| c.time),
        pass_reaction.map(|r| r.time),
        runs.passed.iter().filter_map(|r| r.end.or(r.start)).max(),
    ]
    .into_iter()
    .flatten()
    .max();
    let reaction_time = pending_reaction.map(|r| r.time);
    if unable > completion && reaction_time <= unable && pending <= unable {
        return result(Pending, format!("{login} was unable to review head {head}"));
    }
    let pending_message = || format!("{login} review is in progress for head {head}");
    let reaction_message =
        |value: &str| format!("{login} review is in progress (reaction: {value}) for head {head}");
    if completion.is_some() || !runs.passed.is_empty() {
        if reaction_time > completion || pending > completion {
            if reaction_time > completion && reaction_time >= pending {
                return result(
                    Pending,
                    reaction_message(&pending_reaction.unwrap().content),
                );
            }
            return result(Pending, pending_message());
        }
        if let Some(review) = review {
            if review.state == "APPROVED" {
                return result(Passed, format!("{login} approved head {head}"));
            }
            return result(
                Passed,
                format!("{login} reviewed head {head} with no findings"),
            );
        }
        if passed.is_some() {
            return result(
                Passed,
                format!("{login} completed review with no findings on head {head}"),
            );
        }
        if issue.is_some() {
            return result(
                Passed,
                format!("{login} reported no findings on head {head}"),
            );
        }
        if !runs.passed.is_empty() {
            let names = runs
                .passed
                .iter()
                .map(|r| r.name.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            return result(
                Passed,
                format!("check-run {names} for {login} passed on head {head}"),
            );
        }
        return result(
            Passed,
            format!(
                "{login} reacted with {} on head {head}",
                pass_reaction.unwrap().content
            ),
        );
    }
    if pending.is_some() {
        return result(Pending, pending_message());
    }
    if let Some(reaction) = pending_reaction {
        return result(Pending, reaction_message(&reaction.content));
    }
    if let Some((_, _, stale)) = facts.stale.iter().max_by_key(|(time, id, _)| (*time, *id)) {
        return result(
            Pending,
            format!(
                "last review from {login} is stale (commit {}); waiting for head {head}",
                &stale[..stale.len().min(12)]
            ),
        );
    }
    if facts.stale_reactions {
        return result(
            Pending,
            format!("last reaction from {login} predates head {head}; waiting for current review"),
        );
    }
    result(
        Pending,
        format!("no review activity from {login} for head {head}"),
    )
}
