use super::{Error, Lens, Snapshot, evaluate, in_progress, remote};
use crate::{
    agent_review::{Aggregate, Outcome, Policy},
    gh::{Client, Context, PullRequest},
    policies::triage::TriagePolicy,
};
use serde_json::Value;
use std::time::{Duration, Instant};

pub struct Checked {
    pub snapshot: Snapshot,
    pub aggregate: Aggregate,
    pub admission: Option<Value>,
    pub submitted: Option<Value>,
}
impl Checked {
    pub fn offline(policy: &Policy, snapshot: Snapshot) -> Result<Self, Error> {
        let aggregate = evaluate(policy, &snapshot)?;
        Ok(Self {
            snapshot,
            aggregate,
            admission: None,
            submitted: None,
        })
    }
    pub fn valid(&self) -> bool {
        self.aggregate.state == Outcome::Passed
            && self
                .admission
                .as_ref()
                .is_none_or(|a| a["admission"]["decision"] == "allow")
    }
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub enum Submit {
    #[default]
    None,
    Approve,
    RequestChanges,
    Both,
}
impl Submit {
    fn event(self, outcome: Outcome) -> Option<&'static str> {
        match (self, outcome) {
            (Self::Approve | Self::Both, Outcome::Passed) => Some("APPROVE"),
            (Self::RequestChanges | Self::Both, Outcome::Failed) => Some("REQUEST_CHANGES"),
            _ => None,
        }
    }
}
/// Check an explicitly selected live PR. A non-default submission mode enables a
/// commit-bound write only after repeating evidence collection and policy evaluation.
pub fn check_live(
    policy: &Policy,
    reference: PullRequest,
    wait: Option<u64>,
    submit: Submit,
    triage: Option<&TriagePolicy>,
) -> Result<Checked, Error> {
    if wait.is_some_and(|n| n > 600) {
        return Err(Error::Input(
            "--wait-seconds must be an integer between 0 and 600".into(),
        ));
    }
    if matches!(submit, Submit::Approve | Submit::Both) && triage.is_none() {
        return Err(Error::Input(
            "APPROVE submission requires an explicit trusted --triage-policy".into(),
        ));
    }
    if triage.is_some() && submit == Submit::None {
        return Err(Error::Input(
            "--triage-policy requires explicit --submit-review".into(),
        ));
    }
    let configs = policy.lenses();
    let mut include_runs = false;
    for config in configs {
        include_runs |= !Lens::from(config)?.runs.is_empty();
    }
    let seconds = |key: &str| {
        configs
            .iter()
            .filter_map(|l| l[key].as_u64())
            .max()
            .unwrap_or(0)
    };
    let budget = Duration::from_secs(wait.unwrap_or_else(|| seconds("wait_seconds")));
    let digest = policy.digest();
    let client = Client::open(reference.clone(), Context::Review)?;
    std::thread::sleep(Duration::from_secs(seconds("delay_seconds")));
    let collect = || {
        remote::collect(&reference, &digest, include_runs, &mut |e| client.get(e)).map(|(s, _)| s)
    };
    let mut checked = Checked::offline(policy, collect()?)?;
    let start = Instant::now();
    while start.elapsed() < budget
        && checked.aggregate.state == Outcome::Pending
        && !checked.snapshot.draft
        && (start.elapsed() < Duration::from_secs(30)
            || in_progress(&checked.snapshot, &checked.aggregate, policy))
    {
        std::thread::sleep(Duration::from_secs(15).min(budget.saturating_sub(start.elapsed())));
        checked = Checked::offline(policy, collect()?)?;
    }
    let Some(event) = submit
        .event(checked.aggregate.state)
        .filter(|_| !checked.snapshot.draft)
    else {
        return Ok(checked);
    };
    if checked.snapshot.state != "open" {
        return Err(Error::Input(
            "cannot submit a pull-request review on a closed pull request".into(),
        ));
    }
    let (fresh, admission) = if let Some(admission_policy) = triage {
        let (fresh, admission) =
            crate::triage::collect_for_review(reference.clone(), admission_policy, |get| {
                let (snapshot, reviews) = remote::collect(&reference, &digest, include_runs, get)?;
                let head = snapshot.head.clone();
                let base = snapshot.base.clone();
                Ok((snapshot, reviews, head, base))
            })?;
        (fresh, Some(admission))
    } else {
        (collect()?, None)
    };
    let aggregate = evaluate(policy, &fresh)?;
    if fresh.head != checked.snapshot.head
        || fresh.base != checked.snapshot.base
        || fresh.state != "open"
        || fresh.draft
        || submit.event(aggregate.state) != Some(event)
    {
        return Err(Error::Input(
            "PR review boundary or outcome changed before submission".into(),
        ));
    }
    let submitted = if event != "APPROVE"
        || admission
            .as_ref()
            .is_some_and(|a| a["admission"]["decision"] == "allow")
    {
        Some(remote::submit(&client, &fresh, event)?)
    } else {
        None
    };
    Ok(Checked {
        snapshot: fresh,
        aggregate,
        admission,
        submitted,
    })
}
