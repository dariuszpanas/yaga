//! Tokenless GitHub pull request commit policy, including offline signature verification.
mod dependabot;
mod event;
mod signature;
#[cfg(test)]
mod tests;
use crate::commit_source::{self, Checked, CommitTarget};
use crate::commits::{self, CheckResult, DependabotPullRequests, LoadedConfig, PullRequestMessage};
use crate::fs::{absolute_path, resolve_user_path};
use crate::git::Repository;
pub use dependabot::{
    candidates as dependabot_candidates,
    candidates_with_unicode as dependabot_candidates_with_unicode,
};
pub use event::{
    EventScalar, EventValue, PullRequestEvent, RunnerContext, load_event, parse_event,
    parse_event_fields, parse_event_with_message_limit,
};
pub use signature::{FINGERPRINT, matches_object, split_signature, valid_status};
use std::path::{Path, PathBuf};

pub type Failure = (&'static str, String);
pub const DEPENDABOT_REASON: &str = "Dependabot pull request";
const AUTHOR_REASON: &str = "Configured pull request author";

/// An explicit trusted blob and independently supplied runner identity for a target event.
pub struct TrustedPolicy<'a> {
    pub path: &'a str,
    pub revision: &'a str,
    pub runner_ref: &'a str,
}

pub struct Report {
    pub event: PullRequestEvent,
    /// Title, optional proposed message, then every selected commit oldest first.
    pub results: Vec<Checked>,
    pub has_proposed_message: bool,
    pub config_path: Option<PathBuf>,
}
impl Report {
    pub fn valid(&self) -> bool {
        self.results.iter().all(|r| r.result.valid())
    }
}

fn trusted_config(repo: &Repository, policy: &TrustedPolicy<'_>) -> Result<LoadedConfig, Failure> {
    if !event::valid_oid(policy.revision) {
        return Err((
            "input",
            "trusted policy revision must be a full commit SHA".into(),
        ));
    }
    let path = policy.path;
    let parts = path.split('/').collect::<Vec<_>>();
    if path.len() > 1024
        || parts.len() > 64
        || !matches!(parts.last().copied(), Some(".yaga.toml" | "pyproject.toml"))
        || parts.iter().any(|part| {
            part.is_empty()
                || matches!(*part, "." | "..")
                || !part
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
        })
    {
        return Err((
            "input",
            "trusted policy must be a canonical relative .yaga.toml or pyproject.toml path".into(),
        ));
    }
    let result = repo
        .run(&["ls-tree", "-z", policy.revision, "--", path], 4096)
        .map_err(|e| (e.kind(), e.to_string()))?;
    let entry = || {
        let data = result.stdout.strip_suffix(&[0])?;
        let tab = data.iter().position(|&b| b == b'\t')?;
        if &data[tab + 1..] != path.as_bytes() {
            return None;
        }
        let fields = std::str::from_utf8(&data[..tab]).ok()?;
        let sha = fields
            .strip_prefix("100644 blob ")
            .or_else(|| fields.strip_prefix("100755 blob "))?;
        event::valid_oid(sha).then_some(sha)
    };
    let sha = entry().filter(|_| result.status.success()).ok_or((
        "input",
        "trusted policy must be one committed regular file".into(),
    ))?;
    let blob = repo
        .run(&["cat-file", "blob", sha], commits::MAX_CONFIG_BYTES)
        .map_err(|e| (e.kind(), e.to_string()))?;
    if !blob.status.success() {
        return Err(("input", "trusted policy blob could not be read".into()));
    }
    commits::parse_config(&blob.stdout, &repo.directory().join(path))
        .map_err(|e| ("configuration", e))
}

fn checked(
    target: CommitTarget,
    reason: Option<&'static str>,
    loaded: &LoadedConfig,
    repo: &Repository,
    title: bool,
    trusted: bool,
) -> Result<Checked, Failure> {
    if let Some(reason) = reason {
        let message = commits::normalize_message(&target.message);
        return Ok(Checked {
            source: target.source,
            sha: target.sha,
            result: CheckResult {
                header: message.split('\n').next().unwrap_or_default().into(),
                skipped_reason: Some(reason),
                diagnostics: vec![],
            },
        });
    }
    commit_source::check_target(target, loaded, repo.directory(), title, trusted)
        .map_err(|e| ("input", e))
}

pub fn check(
    event_file: &Path,
    directory: &Path,
    config: Option<&Path>,
    context: Option<&RunnerContext<'_>>,
    trusted: Option<&TrustedPolicy<'_>>,
) -> Result<Report, Failure> {
    // The Action receives bound runner metadata. Only installed callers
    // provide account-qualified CLI paths for the event and checkout.
    let resolve = if context.is_some() {
        absolute_path
    } else {
        resolve_user_path
    };
    let event = event::read_event(resolve(event_file)).map_err(|e| ("input", e))?;
    if trusted.is_some()
        && (config.is_some() || context.is_none_or(|c| c.event_name != "pull_request_target"))
    {
        return Err((
            "input",
            "trusted policy requires pull_request_target runner context and no --config".into(),
        ));
    }
    let directory = resolve(directory)
        .ok()
        .filter(|p| p.is_dir())
        .ok_or(("input", "repository directory does not exist".into()))?;
    if let Some(ctx) = context {
        ctx.validate(&event, trusted.is_some())
            .map_err(|e| ("input", e))?
    }
    let repo = Repository::open(&directory).map_err(|e| (e.kind(), e.to_string()))?;
    let read = |revision: &str| -> Result<String, Failure> {
        let targets = commit_source::collect(&repo, revision, false, 1).map_err(|e| ("git", e))?;
        targets
            .into_iter()
            .next()
            .and_then(|t| t.sha)
            .ok_or(("git", "git returned an incomplete commit record".into()))
    };
    let checkout = read("HEAD")?;
    let loaded = if let Some(trusted) = trusted {
        if event
            .default_branch
            .as_ref()
            .is_none_or(|b| trusted.runner_ref != format!("refs/heads/{b}"))
        {
            return Err((
                "input",
                "trusted policy requires the default-branch runner ref".into(),
            ));
        }
        if checkout != trusted.revision {
            return Err((
                "git",
                "trusted checkout HEAD does not match the runner SHA".into(),
            ));
        }
        if read(&format!("refs/remotes/pull/{}/head", event.number))? != event.head_sha {
            return Err((
                "git",
                "fetched pull request head does not match the event head SHA".into(),
            ));
        }
        trusted_config(&repo, trusted)?
    } else {
        if checkout != event.head_sha {
            return Err((
                "git",
                "repository HEAD does not match the pull request head SHA".into(),
            ));
        }
        commits::load_config(config, &directory, commits::global_config_path().as_deref())
            .map_err(|e| ("configuration", e))?
    };
    let targets = commit_source::collect(
        &repo,
        &format!("{}..{}", event.base_sha, event.head_sha),
        true,
        loaded.max_commits,
    )
    .map_err(|e| ("git", e))?;
    let trusted = trusted.is_some();
    let eligible = loaded.dependabot_pull_requests == DependabotPullRequests::Skip
        && event.author_login == "dependabot[bot]"
        && event.author_type == "Bot"
        && (!trusted
            || (event.head_repository == event.repository
                && event.head_ref.starts_with("dependabot/")));
    let configured = loaded
        .skip_pull_request_authors
        .iter()
        .any(|login| login.eq_ignore_ascii_case(&event.author_login));
    let filtered = if eligible && !configured {
        dependabot::filter(
            &repo,
            &event,
            &targets,
            loaded.dependabot_require_github_signature,
        )
        .map_err(|e| ("git", e))?
    } else {
        Default::default()
    };
    let reason = if eligible && filtered.len() == targets.len() {
        Some(DEPENDABOT_REASON)
    } else {
        configured.then_some(AUTHOR_REASON)
    };
    let has_proposed_message = loaded.pull_request_message == PullRequestMessage::TitleAndBody;
    let proposed = if has_proposed_message {
        Some(checked(
            CommitTarget::literal(
                format!("pull request #{} proposed message", event.number),
                format!("{}\n\n{}", event.title, event.body),
            ),
            reason,
            &loaded,
            &repo,
            false,
            trusted,
        )?)
    } else {
        None
    };
    let title = checked(
        CommitTarget::literal(
            format!("pull request #{} title", event.number),
            event.title.clone(),
        ),
        reason,
        &loaded,
        &repo,
        true,
        trusted,
    )?;
    let mut results = vec![title];
    results.extend(proposed);
    for target in targets {
        let reason = if configured {
            Some(AUTHOR_REASON)
        } else {
            target
                .sha
                .as_ref()
                .filter(|s| filtered.contains(*s))
                .map(|_| DEPENDABOT_REASON)
        };
        results.push(checked(target, reason, &loaded, &repo, false, trusted)?)
    }
    Ok(Report {
        event,
        results,
        has_proposed_message,
        config_path: loaded.path,
    })
}
