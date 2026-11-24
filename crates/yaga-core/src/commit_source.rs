//! Resolve one commit or an explicit range using the bounded shared Git runtime.

use crate::git::{Repository, parse_object_id, safe_git_error};
use regex::Regex;
use std::sync::LazyLock;

pub struct CommitTarget {
    pub source: String,
    pub sha: Option<String>,
    pub message: String,
    pub is_merge: bool,
}

impl CommitTarget {
    pub fn literal(source: String, message: String) -> Self {
        Self {
            source,
            message,
            sha: None,
            is_merge: false,
        }
    }
}

static UNSAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\s\p{Cc}\p{Cf}]").unwrap());
static SET_OPERATOR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\^(?:@|!|-)").unwrap());

fn validate_revision(value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.starts_with('-')
        || value.chars().count() > 512
        || UNSAFE.is_match(value)
    {
        return Err("revision must be one bounded, non-option Git revision expression".into());
    }
    Ok(())
}

fn resolve(repo: &Repository, revision: &str, source: &str) -> Result<String, String> {
    validate_revision(revision)?;
    if revision.starts_with('^')
        || revision.contains(':')
        || revision.contains("..")
        || SET_OPERATOR.is_match(revision)
    {
        return Err(format!(
            "{source} requires exactly one commit-ish, not a path or revision set"
        ));
    }
    let result = repo
        .run(
            &[
                "rev-parse",
                "--verify",
                "--end-of-options",
                &format!("{revision}^{{commit}}"),
            ],
            256,
        )
        .map_err(|e| e.to_string())?;
    if !result.status.success() {
        return Err(format!(
            "git could not resolve {} as one commit: {}",
            crate::text::python_repr(revision),
            safe_git_error(&result.stderr)
        ));
    }
    parse_object_id(&result.stdout, "commit").map_err(|e| e.to_string())
}

pub fn collect(
    repo: &Repository,
    value: &str,
    range: bool,
    maximum: usize,
) -> Result<Vec<CommitTarget>, String> {
    if range && maximum == 0 {
        return Err("maximum commit count must be positive".into());
    }
    let (shallow, grafts) = if range {
        (
            "--range requires a non-shallow repository with complete history",
            "--range requires complete history without legacy graft overlays",
        )
    } else {
        (
            "commit selection requires a non-shallow repository with complete history",
            "commit selection requires complete history without legacy graft overlays",
        )
    };
    let history = || {
        repo.require_complete_history(shallow, grafts)
            .map_err(|e| e.to_string())
    };
    let selection = if range {
        validate_revision(value)?;
        let Some((base, right)) = value.split_once("..") else {
            return Err("--range requires explicit non-empty base and head revisions".into());
        };
        let (separator, head) = right
            .strip_prefix('.')
            .map_or(("..", right), |s| ("...", s));
        if base.is_empty() || head.is_empty() {
            return Err("--range requires explicit non-empty base and head revisions".into());
        }
        if head.starts_with('.') || base.ends_with('.') || head.contains("..") {
            return Err("--range requires exactly one two-dot or three-dot separator".into());
        }
        history()?;
        format!(
            "{}{separator}{}",
            resolve(repo, base, "--range base")?,
            resolve(repo, head, "--range head")?
        )
    } else {
        let sha = resolve(repo, value, "--commit")?;
        history()?;
        sha
    };
    let count = if range {
        maximum
            .checked_add(1)
            .ok_or("maximum commit count is invalid")?
    } else {
        1
    };
    let result = repo
        .run(
            &[
                "log",
                "--no-show-signature",
                "--encoding=UTF-8",
                "--format=%H%x00%P%x00%B",
                "-z",
                &format!("--max-count={count}"),
                if range { "--reverse" } else { "--no-walk" },
                &selection,
                "--",
            ],
            16 * 1024 * 1024,
        )
        .map_err(|e| e.to_string())?;
    if !result.status.success() {
        return Err(format!(
            "git could not read the resolved commit selection: {}",
            safe_git_error(&result.stderr)
        ));
    }
    if result.stdout.is_empty() {
        history()?;
        let label = crate::text::python_repr(value);
        return Err(if range {
            format!("revision range {label} selected no commits")
        } else {
            format!("revision {label} did not select a commit")
        });
    }
    let mut commits = Vec::new();
    let data = result
        .stdout
        .strip_suffix(&[0])
        .ok_or("git returned an incomplete commit record")?;
    let mut fields = data.split(|byte| *byte == 0);
    while let Some(raw) = fields.next() {
        let sha = parse_object_id(raw, "commit").map_err(|e| e.to_string())?;
        let parent_field = fields
            .next()
            .ok_or("git returned an incomplete commit record")?;
        let message = fields
            .next()
            .ok_or("git returned an incomplete commit record")?;
        let mut count = 0;
        if !parent_field.is_empty() {
            for parent in parent_field.split(|b| *b == b' ') {
                if parent.len() != sha.len() {
                    return Err("git returned a malformed parent identity".into());
                }
                parse_object_id(parent, "parent").map_err(|e| e.to_string())?;
                count += 1;
            }
        }
        commits.push(CommitTarget {
            source: format!("commit {}", &sha[..12]),
            sha: Some(sha),
            message: std::str::from_utf8(message)
                .map_err(|_| "git commit message is not valid UTF-8")?
                .into(),
            is_merge: count > 1,
        });
        if range && commits.len() > maximum {
            return Err(format!(
                "commit selection exceeds the configured limit of {maximum}"
            ));
        }
    }
    history()?;
    Ok(commits)
}

use crate::commits::{self, CheckResult, LoadedConfig, Severity, TyposConfig, TyposPolicy};
use std::path::Path;

pub struct Checked {
    pub source: String,
    pub sha: Option<String>,
    pub result: CheckResult,
}

pub fn validate_title(value: &str, label: &str) -> Result<(), String> {
    static UNSAFE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Zl}\p{Zp}\p{Cf}&&[^\x{200C}\x{200D}]]").unwrap());
    if value.is_empty() {
        return Err(format!("{label} must be one non-empty line"));
    }
    if value.len() > 1024 {
        return Err(format!("{label} exceeds the hard 1024-byte limit"));
    }
    if UNSAFE.is_match(value) {
        return Err(format!("{label} contains unsafe characters"));
    }
    Ok(())
}

pub fn check_target(
    target: CommitTarget,
    loaded: &LoadedConfig,
    directory: &Path,
    header_only: bool,
    isolated: bool,
) -> Result<Checked, String> {
    let mut result = commits::check_message(
        &target.message,
        &loaded.policy,
        target.is_merge,
        header_only,
    );
    if loaded.typos == TyposPolicy::Check && result.skipped_reason.is_none() {
        let severity = if loaded
            .policy
            .warning_rules
            .iter()
            .any(|s| s == "typos.word")
        {
            Severity::Warning
        } else {
            Severity::Error
        };
        result.diagnostics.extend(crate::typos::check(
            directory,
            &target.message,
            isolated || loaded.typos_config == TyposConfig::Isolated,
            severity,
        )?);
    }
    Ok(Checked {
        source: target.source,
        sha: target.sha,
        result,
    })
}
