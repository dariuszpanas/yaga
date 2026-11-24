//! Invoke pinned actionlint in isolated, temporary Docker resources.
mod cleanup;
mod embedded;
mod runtime;
mod snapshot;
pub use embedded::{LifecycleError, PreparedLint, VettedDocker, lint_prepared};
pub use snapshot::{PreparedWorkflow, prepare_workflow};
#[cfg(test)]
mod tests;

use crate::workflows::{Diagnostic, WorkflowInput};
use crate::{fs::resolve_user_path, json::strict_value, text::safe_text};
use regex::Regex;
use std::path::Path;
use std::sync::{
    OnceLock,
    atomic::{AtomicBool, Ordering},
};
use std::time::{Duration, Instant};

pub type LintResults = Vec<(String, Vec<Diagnostic>)>;
static CLI_CANCELLED: AtomicBool = AtomicBool::new(false);
static SIGNAL_HANDLERS: OnceLock<Result<(), String>> = OnceLock::new();

/// Install cleanup-aware termination handlers in a standalone CLI process.
/// Embedders can pass their own cancellation flag to `lint_inputs_cancellable`.
pub fn install_cli_interrupt_handler() -> Result<(), String> {
    SIGNAL_HANDLERS
        .get_or_init(|| {
            ctrlc::try_set_handler(|| CLI_CANCELLED.store(true, Ordering::Relaxed))
                .map_err(|_| "actionlint termination handlers could not be registered".into())
        })
        .clone()
}

pub fn lint_inputs(repo: &Path, inputs: &[WorkflowInput]) -> Result<LintResults, String> {
    lint_inputs_cancellable(repo, inputs, &CLI_CANCELLED)
}

pub fn lint_inputs_cancellable(
    repo: &Path,
    inputs: &[WorkflowInput],
    cancel: &AtomicBool,
) -> Result<LintResults, String> {
    let repo =
        resolve_user_path(repo).map_err(|_| "workflow repository path cannot be resolved")?;
    if !repo.is_dir() {
        return Err("workflow repository directory does not exist".into());
    }
    let deadline = Instant::now() + Duration::from_secs(300);
    let snapshot = snapshot::Snapshot::build(&repo, inputs)?;
    let cancelled = || cancel.load(Ordering::Relaxed);
    let docker = runtime::Docker::discover(&repo, &cancelled)?;
    let archive = snapshot.archive()?;
    let mut results = Vec::with_capacity(inputs.len());
    docker
        .run(
            archive,
            &snapshot,
            inputs.iter().map(|input| input.relative_path.as_str()),
            deadline,
            |path, bytes| {
                let findings = diagnostics(bytes, path)?;
                let count = findings.len();
                results.push((path.to_owned(), findings));
                Ok(count)
            },
        )
        .map_err(|error| error.to_string())?;
    Ok(results)
}

/// Validate and normalize one bounded actionlint response for an exact selected workflow.
pub fn diagnostics(bytes: &[u8], path: &str) -> Result<Vec<Diagnostic>, String> {
    if bytes.len() > 2 * 1024 * 1024 {
        return Err("actionlint output exceeds the hard 2097152-byte limit".into());
    }
    const KEYS: [&str; 5] = ["message", "filepath", "line", "column", "kind"];
    let value = strict_value(bytes).map_err(|_| "actionlint returned malformed JSON output")?;
    let array = value
        .as_array()
        .ok_or("actionlint JSON output must be a list")?;
    if array.len() > 512 {
        return Err("actionlint findings exceed the hard 512-diagnostic limit".into());
    }
    let mut diagnostics = Vec::with_capacity(array.len());
    for item in array {
        let d = item
            .as_object()
            .ok_or("actionlint returned a malformed diagnostic")?;
        if !KEYS.iter().all(|k| d.contains_key(*k))
            || d.keys().any(|k| {
                !KEYS.contains(&k.as_str()) && !matches!(k.as_str(), "snippet" | "end_column")
            })
        {
            return Err("actionlint returned an unexpected diagnostic shape".into());
        }
        if d["filepath"].as_str() != Some(path) {
            return Err("actionlint returned an unexpected diagnostic path".into());
        }
        if d.get("snippet").is_some_and(|s| !s.is_string()) {
            return Err("actionlint returned a malformed diagnostic field".into());
        }
        let field = |key: &str| {
            d[key]
                .as_u64()
                .filter(|&n| n <= 1_048_577)
                .map(|n| (n as usize).max(1))
                .ok_or_else(|| format!("actionlint returned an invalid diagnostic {key}"))
        };
        let line = field("line")?;
        let column = field("column")?;
        if d.contains_key("end_column") {
            field("end_column")?;
        }
        let message = d["message"]
            .as_str()
            .ok_or("actionlint returned a malformed diagnostic field")?;
        let message = safe_text(&message.chars().take(2000).collect::<String>(), 500);
        let kind = d["kind"]
            .as_str()
            .ok_or("actionlint returned a malformed diagnostic field")?;
        let kind = safe_text(&kind.chars().take(256).collect::<String>(), 64).to_lowercase();
        static NON_KIND: std::sync::LazyLock<Regex> =
            std::sync::LazyLock::new(|| Regex::new("[^a-z0-9]+").unwrap());
        let normalized = NON_KIND.replace_all(&kind, "-");
        let normalized = normalized.trim_matches('-');
        diagnostics.push(Diagnostic {
            line,
            column,
            code: format!(
                "actionlint.{}",
                if normalized.is_empty() {
                    "unknown"
                } else {
                    normalized
                }
            ),
            message: if message.is_empty() {
                "actionlint finding".into()
            } else {
                message
            },
        });
    }
    diagnostics.sort_by(|a, b| {
        (a.line, a.column, &a.code, &a.message).cmp(&(b.line, b.column, &b.code, &b.message))
    });
    Ok(diagnostics)
}
