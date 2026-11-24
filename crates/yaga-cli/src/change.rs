//! The explicit changed-path range command; planning and evaluation never run bindings.
use crate::Format;
use crate::trees::{Failure, annotation, finish, pretty, read_policy};
use clap::{Args, Subcommand};
use serde_json::json;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use yaga_core::git::read_changed_paths;
use yaga_core::paths::changes::{ChangePolicy, RuleResult, Status};
use yaga_core::text::{json_text, safe_text};

#[derive(Args)]
pub struct ChangeArgs {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Require configured path changes for an explicit Git range.
    Check {
        #[arg(long)]
        policy: PathBuf,
        #[arg(long, allow_hyphen_values = true)]
        range: String,
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        #[arg(long, value_enum, default_value = "text", ignore_case = true)]
        format: Format,
        #[arg(short, long)]
        quiet: bool,
    },
}

fn preview(values: &[&str], maximum: usize) -> String {
    let mut result = values
        .iter()
        .take(8)
        .map(|s| safe_text(s, maximum))
        .collect::<Vec<_>>()
        .join(", ");
    if values.len() > 8 {
        result.push_str(&format!(" … (+{} more)", values.len() - 8));
    }
    result
}

fn reported_paths(values: &[&str], count: usize) -> Vec<String> {
    values
        .iter()
        .take(count)
        .map(|s| json_text(s, 1000))
        .collect()
}

fn status(rule: &RuleResult<'_>) -> &'static str {
    match rule.status() {
        Status::Skipped => "skipped",
        Status::Passed => "passed",
        Status::Failed => "failed",
    }
}

fn check(
    policy: &Path,
    repo: &Path,
    range: &str,
    format: Format,
    quiet: bool,
) -> Result<(bool, String), Failure> {
    let (policy_path, bytes) = read_policy(policy, "change")?;
    let policy = ChangePolicy::from_toml_at(&bytes, Some(&policy_path.to_string_lossy()))
        .map_err(|e| Failure("configuration", e.to_string()))?;
    let selected = read_changed_paths(repo, range, false)?;
    let paths: Vec<_> = selected.paths.iter().map(String::as_str).collect();
    let rules = policy
        .check(&paths)
        .map_err(|e| Failure("configuration", e.to_string()))?;
    let passed = rules
        .iter()
        .filter(|r| r.status() == Status::Passed)
        .count();
    let skipped = rules
        .iter()
        .filter(|r| r.status() == Status::Skipped)
        .count();
    let failed = rules.len() - passed - skipped;
    let valid = failed == 0;
    if quiet {
        return Ok((valid, String::new()));
    }
    let code = RuleResult::CODE;
    if matches!(format, Format::Json) {
        let documents: Vec<_> = rules.iter().map(|r| json!({"name":r.name,"status":status(r),"code":(r.status()==Status::Failed).then_some(code),
            "when_any":r.when_any,"require_any":r.require_any,"triggered_paths":reported_paths(&r.triggered_paths,32),"triggered_paths_omitted":r.triggered_paths.len().saturating_sub(32),
            "required_paths":reported_paths(&r.required_paths,32),"required_paths_omitted":r.required_paths.len().saturating_sub(32)})).collect();
        let doc = json!({
            "schema_version": 1,
            "kind": "change_policy",
            "status": if valid {"passed"} else {"failed"},
            "valid": valid,
            "policy_path": json_text(&policy_path.to_string_lossy(),1000),
            "range": json_text(range,300),
            "base_sha": selected.base_sha,
            "head_sha": selected.head_sha,
            "comparison_sha": selected.comparison_sha,
            "paths_changed": paths.len(),
            "paths": reported_paths(&paths,64),
            "paths_omitted": paths.len().saturating_sub(64),
            "rules": documents,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        });
        return Ok((valid, pretty(&doc)));
    }
    let mut lines = Vec::new();
    let title = if matches!(format, Format::Text) {
        lines.push(format!(
            "Policy: {}",
            safe_text(&policy_path.to_string_lossy(), 1000)
        ));
        lines.push(format!("Range: {}", safe_text(range, 300)));
        for r in &rules {
            lines.push(format!("{:7} {}", status(r).to_ascii_uppercase(), r.name));
            if r.status() == Status::Failed {
                lines.push(format!(
                    "         [{code}] no changed path matched require-any: {}",
                    preview(&r.require_any, 512)
                ));
            }
            if !r.triggered_paths.is_empty() {
                lines.push(format!(
                    "         triggered: {}",
                    preview(&r.triggered_paths, 1000)
                ));
            }
            if !r.required_paths.is_empty() {
                lines.push(format!(
                    "         required matches: {}",
                    preview(&r.required_paths, 1000)
                ));
            }
        }
        "Change"
    } else {
        for r in rules.iter().filter(|r| r.status() == Status::Failed) {
            lines.push(annotation(
                code,
                Some(r.triggered_paths[0]),
                &format!(
                    "rule '{}' requires at least one changed path matching: {}",
                    r.name,
                    preview(&r.require_any, 512)
                ),
            ));
        }
        "YAGA change"
    };
    lines.push(format!("{title} policy: {} rule(s); {passed} passed, {failed} failed, {skipped} skipped; {} changed path(s).",rules.len(),paths.len()));
    Ok((valid, lines.join("\n")))
}

pub fn run(args: ChangeArgs) -> ExitCode {
    let Command::Check {
        policy,
        range,
        repo,
        format,
        quiet,
    } = args.command;
    finish(
        "change",
        format,
        quiet,
        check(&policy, &repo, &range, format, quiet),
    )
}
