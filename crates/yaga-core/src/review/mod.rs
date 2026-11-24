//! Installed pull-request review checks over explicit policies and bounded evidence.
mod evaluate;
mod evidence;
mod markers;
mod remote;
mod service;
mod snapshot;
#[cfg(test)]
mod tests;
use crate::agent_review::{Aggregate, Outcome, Policy};
use serde_json::Value;
pub use service::{Checked, Submit, check_live};
pub use snapshot::Snapshot;
use std::{collections::HashMap, fmt};

#[derive(Debug)]
pub enum Error {
    Input(String),
    Configuration(String),
}
impl Error {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Input(_) => "input",
            Self::Configuration(_) => "configuration",
        }
    }
}
impl From<String> for Error {
    fn from(s: String) -> Self {
        Self::Input(s)
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Input(s) | Self::Configuration(s) => s,
        })
    }
}
impl std::error::Error for Error {}

/// Identity and evidence sources for a review lens whose bot has already been resolved.
pub struct Lens<'a> {
    pub name: &'a str,
    pub login: &'a str,
    pub actor_names: Vec<&'a str>,
    pub user: Option<u64>,
    pub app: Option<u64>,
    pub pass: Vec<&'a str>,
    pub pending: Vec<&'a str>,
    pub runs: Vec<&'a str>,
}
fn list<'a>(lens: &'a Value, key: &str, defaults: &[&'a str]) -> Vec<&'a str> {
    lens[key]
        .as_array()
        .map(|v| v.iter().map(|s| s.as_str().unwrap()).collect())
        .unwrap_or_else(|| defaults.to_vec())
}
impl<'a> Lens<'a> {
    fn from(lens: &'a Value) -> Result<Self, Error> {
        const CODEX: &str = "chatgpt-codex-connector[bot]";
        const COPILOT: &str = "copilot-pull-request-reviewer[bot]";
        let name = lens["name"].as_str().unwrap();
        let preset = lens["preset"].as_str().unwrap();
        let mut user = lens["bot_user_id"].as_u64();
        let mut app = lens["bot_app_id"].as_u64();
        let default_login = match preset {
            "codex" => Some(CODEX),
            "copilot" => Some(COPILOT),
            "claude" => Some("claude[bot]"),
            _ => None,
        };
        let login = lens["bot_login"].as_str().or(default_login).ok_or_else(|| {
            Error::Configuration(format!(
                "Agent review lens {name} with preset '{preset}' requires explicit bot-login for pull-request review checks"
            ))
        })?;
        let mut alias = None;
        if preset == "codex" && login.eq_ignore_ascii_case(CODEX) {
            user.get_or_insert(199175422);
            app.get_or_insert(1144995);
        }
        if preset == "copilot"
            && (login.eq_ignore_ascii_case(COPILOT) || login.eq_ignore_ascii_case("Copilot"))
        {
            user.get_or_insert(175728472);
            alias = Some(if login.eq_ignore_ascii_case(COPILOT) {
                "Copilot"
            } else {
                COPILOT
            });
        }
        Ok(Self {
            name,
            login,
            actor_names: std::iter::once(login).chain(alias).collect(),
            user,
            app,
            pass: list(lens, "pass_reactions", &["+1"]),
            pending: list(lens, "pending_reactions", &["eyes"]),
            runs: list(lens, "check_runs", &[]),
        })
    }
    /// Evaluate one resolved lens over the bounded snapshot without aggregating a policy.
    pub fn evaluate(&self, snapshot: &Snapshot) -> Result<crate::agent_review::LensResult, String> {
        evaluate::lens(self, snapshot)
    }
    fn matches(&self, actor: &snapshot::Actor, kind: &str) -> Result<bool, String> {
        if !self
            .actor_names
            .iter()
            .any(|name| name.eq_ignore_ascii_case(&actor.login))
        {
            return Ok(false);
        }
        if self.user.is_some() && self.user != actor.id {
            return Err(format!(
                "{kind} for {} has an unexpected author_id",
                self.login
            ));
        }
        Ok(true)
    }
}
pub fn evaluate(policy: &Policy, snapshot: &Snapshot) -> Result<Aggregate, Error> {
    let digest = policy.digest();
    if snapshot.plan.as_deref() != Some(&digest) {
        return Err(Error::Configuration(
            "pull-request review snapshot plan_digest does not match the current policy plan"
                .into(),
        ));
    }
    let mut known = HashMap::new();
    let mut lenses = Vec::new();
    for config in policy.lenses() {
        let lens = Lens::from(config)?;
        for login in &lens.actor_names {
            if let Some(previous) = known.insert(login.to_ascii_lowercase(), lens.name) {
                return Err(Error::Configuration(format!(
                    "Agent review lenses {previous} and {} resolve to the same bot-login ({}); pull-request snapshot evaluation requires distinct bot identities per lens",
                    lens.name, lens.login
                )));
            }
        }
        lenses.push(lens);
    }
    let results = lenses
        .iter()
        .map(|lens| lens.evaluate(snapshot))
        .collect::<Result<Vec<_>, _>>()?;
    policy
        .evaluate_receipt(digest, results, "snapshot")
        .map_err(Error::Configuration)
}

pub fn in_progress(snapshot: &Snapshot, aggregate: &Aggregate, policy: &Policy) -> bool {
    if snapshot.draft
        || aggregate.state != Outcome::Pending
        || aggregate.blocking_pending.is_empty()
    {
        return false;
    }
    let suffix = format!("for head {}", &snapshot.head[..12]);
    let mut states = aggregate.blocking_pending.iter().map(|name| {
        aggregate
            .results
            .iter()
            .find(|r| r.lens == *name && r.outcome == Outcome::Pending)
            .and_then(|r| r.summary.as_ref())
            .is_some_and(|s| {
                s.ends_with(&suffix)
                    && (s.contains(" review is in progress ") || s.starts_with("check-run "))
            })
    });
    if policy.aggregation() == "any-required" {
        states.any(|v| v)
    } else {
        states.all(|v| v)
    }
}
