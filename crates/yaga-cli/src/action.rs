//! Closed entry points used by the trusted composite Action.
mod cli;
mod summary;

use crate::{Format, commit_report};
use std::{env, path::Path, process::ExitCode};
use yaga_core::{agent_review::github::runtime, github_commit, text::safe_text};

fn variable(name: &str) -> String {
    env::var(name).unwrap_or_default()
}

fn required(name: &str) -> Result<String, String> {
    let value = variable(name);
    if value.is_empty() {
        return Err(format!("{name} is required for the commit Action"));
    }
    Ok(value)
}

fn commit() -> Result<u8, String> {
    let repository_id = required("GITHUB_REPOSITORY_ID")?;
    let repository_id = repository_id
        .parse::<i64>()
        .ok()
        .filter(|&v| v > 0 && v.to_string() == repository_id)
        .ok_or("GitHub repository ID is invalid")?;
    let context = github_commit::RunnerContext {
        event_name: &required("GITHUB_EVENT_NAME")?,
        repository: &required("GITHUB_REPOSITORY")?,
        repository_id,
        base_ref: &required("GITHUB_BASE_REF")?,
        head_ref: &required("GITHUB_HEAD_REF")?,
    };
    let path = variable("YAGA_COMMIT_TRUSTED_CONFIG");
    let revision;
    let runner_ref;
    let trusted = if path.is_empty() {
        None
    } else {
        revision = required("GITHUB_SHA")?;
        runner_ref = required("GITHUB_REF")?;
        Some(github_commit::TrustedPolicy {
            path: &path,
            revision: &revision,
            runner_ref: &runner_ref,
        })
    };
    match github_commit::check(
        Path::new(&variable("GITHUB_EVENT_PATH")),
        Path::new(&variable("GITHUB_WORKSPACE")),
        None,
        Some(&context),
        trusted.as_ref(),
    ) {
        Ok(report) => {
            println!(
                "{}",
                commit_report::render_pull_request(&report, Format::Github)
            );
            Ok(u8::from(!report.valid()))
        }
        Err((kind, message)) => {
            let message = safe_text(&message, 1000).replace('%', "%25");
            println!("::error title=YAGA commit policy::YAGA {kind} error: {message}");
            Ok(2)
        }
    }
}

fn gate() -> Result<u8, String> {
    if variable("YAGA_GATE") != "agent-review" {
        return Err("gate is invalid".into());
    }
    let start = variable("YAGA_SETUP_START_MS")
        .parse::<u64>()
        .map_err(|_| "invalid Action setup time")?;
    let elapsed = std::time::SystemTime::UNIX_EPOCH
        .checked_add(std::time::Duration::from_millis(start))
        .and_then(|s| s.elapsed().ok())
        .ok_or("invalid Action setup time")?;
    let (code, message) = runtime::run_action_after_setup(&variable("YAGA_OPERATION"), elapsed)?;
    println!("{message}");
    Ok(code)
}

pub fn run() -> ExitCode {
    let args: Vec<_> = env::args_os().skip(2).collect();
    if args.first().and_then(|v| v.to_str()) == Some("summary") {
        return summary::run(&args[1..]);
    }
    let [mode] = args.as_slice() else {
        eprintln!("YAGA failed: action command is invalid");
        return ExitCode::from(2);
    };
    let mode = mode.to_str().unwrap_or("");
    let result = match mode {
        "cli" => cli::run(),
        "commit" => commit(),
        "gate" => gate(),
        _ => Err("action command is invalid".into()),
    };
    match result {
        Ok(code) => ExitCode::from(code),
        Err(message) if mode == "cli" => {
            let message = safe_text(&message, 1000).replace('%', "%25");
            eprintln!("::error title=YAGA action::YAGA input error: {message}");
            ExitCode::from(2)
        }
        Err(message) => {
            eprintln!("YAGA failed: {}", safe_text(&message, 1000));
            ExitCode::from(if mode == "gate" { 1 } else { 2 })
        }
    }
}
