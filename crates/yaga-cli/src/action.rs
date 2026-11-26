//! Closed entry points used by the trusted composite Action.
mod cli;
mod summary;

use crate::{Format, commit_report};
use std::{
    env,
    path::Path,
    process::ExitCode,
    time::{Duration, SystemTime},
};
use yaga_core::{agent_review::github::runtime, github_commit, text::safe_text};

const MAX_CLOCK_SKEW: Duration = Duration::from_secs(5);

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

fn setup_elapsed(raw: &str, now: SystemTime) -> Result<Duration, &'static str> {
    let start = raw
        .parse::<u64>()
        .map_err(|_| "invalid Action setup time")?;
    let started = SystemTime::UNIX_EPOCH
        .checked_add(Duration::from_millis(start))
        .ok_or("invalid Action setup time")?;
    match now.duration_since(started) {
        Ok(elapsed) => Ok(elapsed),
        Err(skew) if skew.duration() <= MAX_CLOCK_SKEW => Ok(Duration::ZERO),
        Err(_) => Err("invalid Action setup time"),
    }
}

fn gate() -> Result<u8, String> {
    if variable("YAGA_GATE") != "agent-review" {
        return Err("gate is invalid".into());
    }
    let elapsed = setup_elapsed(&variable("YAGA_SETUP_START_MS"), SystemTime::now())?;
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
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn setup_elapsed_clamps_small_backward_skew_and_rejects_distant_future() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_700_000_000);
        let past_ms = (1_700_000_000u64 - 12) * 1000;
        assert_eq!(
            setup_elapsed(&past_ms.to_string(), now),
            Ok(Duration::from_secs(12))
        );

        let small_skew_ms = 1_700_000_000u64 * 1000 + 2_500;
        assert_eq!(
            setup_elapsed(&small_skew_ms.to_string(), now),
            Ok(Duration::ZERO)
        );

        let exact_bound_ms = (1_700_000_000u64 + 5) * 1000;
        assert_eq!(
            setup_elapsed(&exact_bound_ms.to_string(), now),
            Ok(Duration::ZERO)
        );

        let far_future_ms = (1_700_000_000u64 + 60) * 1000;
        assert_eq!(
            setup_elapsed(&far_future_ms.to_string(), now),
            Err("invalid Action setup time")
        );
        assert_eq!(
            setup_elapsed("not-a-number", now),
            Err("invalid Action setup time")
        );
    }
}
