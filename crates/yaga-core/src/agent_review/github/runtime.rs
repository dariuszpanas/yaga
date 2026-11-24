//! CLI-only transport adapter for the fixed, trusted GitHub runner environment.
use super::{
    api::RestApi,
    boundary::server_origin,
    bounded,
    gate::{Publisher, invalidate},
    repository_name,
    runs::load_source_from_wake,
    workflow_path,
};
use crate::{fs::read_regular_prefix, json::strict_value};
use serde_json::{Value, json};
use std::{
    env,
    fs::OpenOptions,
    io::Write,
    path::Path,
    time::{Duration, Instant},
};

fn required(name: &str) -> Result<String, String> {
    env::var(name)
        .ok()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| format!("required GitHub environment is missing: {name}"))
}
fn number(name: &str, label: &str) -> Result<i64, String> {
    let raw = required(name)?;
    if raw.bytes().all(|b| b.is_ascii_digit())
        && let Ok(n) = raw.parse::<i64>()
        && n > 0
    {
        return Ok(n);
    }
    Err(format!("{label} must be a bounded positive integer"))
}
fn outputs(route: &str, number: Option<i64>) -> Result<(), String> {
    if !matches!(
        route,
        "approved" | "done" | "external" | "observe" | "owner" | "skip"
    ) {
        return Err("YAGA route is invalid".into());
    }
    let mut data = format!("route={route}\n");
    if let Some(n) = number {
        if n <= 0 {
            return Err("pull request output must be a bounded positive integer".into());
        }
        data.push_str(&format!("pull_request_number={n}\n"));
    }
    OpenOptions::new()
        .append(true)
        .create(true)
        .open(required("GITHUB_OUTPUT")?)
        .and_then(|mut f| f.write_all(data.as_bytes()))
        .map_err(|_| "GITHUB_OUTPUT could not be written".into())
}
fn read_event() -> Result<Value, String> {
    let path = required("GITHUB_EVENT_PATH")?;
    let bytes = read_regular_prefix(Path::new(&path), 4 * 1024 * 1024)
        .map_err(|_| "GitHub event payload could not be read")?;
    if bytes.is_empty() || bytes.len() > 4 * 1024 * 1024 {
        return Err("GitHub event payload is empty or too large".into());
    }
    let value = strict_value(&bytes).map_err(|_| "GitHub event payload is invalid JSON")?;
    if !value.is_object() {
        return Err("GitHub event payload must be an object".into());
    }
    Ok(value)
}
#[cfg(feature = "github-http")]
pub fn run_action(operation: &str) -> Result<(u8, String), String> {
    run_action_after_setup(operation, Duration::ZERO)
}

/// Include trusted Action setup in the existing per-job request and finalization budgets.
#[cfg(feature = "github-http")]
pub fn run_action_after_setup(operation: &str, elapsed: Duration) -> Result<(u8, String), String> {
    let start = Instant::now()
        .checked_sub(elapsed)
        .ok_or("invalid Action setup duration")?;
    run_from(operation, start, |timeout, budget, requests, success| {
        super::api::GitHubRestApi::new(
            &required("GITHUB_TOKEN")?,
            &env::var("GITHUB_API_URL").unwrap_or_else(|_| "https://api.github.com".into()),
            timeout,
            budget,
            Some(requests),
            Some(success),
        )
    })
}

/// Execute the same lifecycle state machine with an embedding host's bounded REST transport.
pub fn run_with_transport<A: RestApi>(
    operation: &str,
    transport: impl FnOnce(Duration, usize, Instant, Instant) -> Result<A, String>,
) -> Result<(u8, String), String> {
    run_from(operation, Instant::now(), transport)
}

fn run_from<A: RestApi>(
    operation: &str,
    start: Instant,
    transport: impl FnOnce(Duration, usize, Instant, Instant) -> Result<A, String>,
) -> Result<(u8, String), String> {
    let budget = match operation {
        "invalidate" => 16,
        "authorize" | "prepare" | "finalize" => 128,
        "request" | "observe" => 260,
        _ => return Err("operation is invalid".into()),
    };
    let repo = required("GITHUB_REPOSITORY")?;
    repository_name(&repo)?;
    let name = bounded(
        &json!(required("GITHUB_EVENT_NAME")?),
        "GitHub event name",
        64,
    )?;
    let event = read_event()?;
    let default = bounded(
        &event["repository"]["default_branch"],
        "default branch",
        255,
    )?;
    let workflow = bounded(
        &json!(required("GITHUB_WORKFLOW_REF")?),
        "GitHub workflow ref",
        512,
    )?;
    let path = workflow
        .strip_prefix(&format!("{repo}/"))
        .and_then(|s| s.strip_suffix(&format!("@refs/heads/{default}")))
        .ok_or("YAGA is not running from the repository default branch")?;
    workflow_path(path, "YAGA workflow path")?;
    if required("GITHUB_REF")? != format!("refs/heads/{default}") {
        return Err("YAGA ref is not the repository default branch".into());
    }
    if env::var_os("YAGA_AGENT_REVIEW_POLICY_FILE").is_some_and(|s| !s.is_empty()) {
        return Err("current GitHub Agent review adapter does not support configured lens policies; use an external adapter with policy plan/evaluate".into());
    }
    let server = required("GITHUB_SERVER_URL")?;
    let origin = server_origin(&server)?;
    let run = number("GITHUB_RUN_ID", "YAGA workflow run ID")?;
    let attempt = number("GITHUB_RUN_ATTEMPT", "YAGA workflow run attempt")?;
    let url = format!("{origin}/{repo}/actions/runs/{run}/attempts/{attempt}");
    let seconds = required("YAGA_REQUEST_TIMEOUT")?
        .trim()
        .parse::<f64>()
        .map_err(|_| "request timeout must be a number")?;
    if !(1.0..=30.0).contains(&seconds) {
        return Err("request timeout must be between 1 and 30 seconds".into());
    }
    let timeout = Duration::from_secs_f64(seconds);
    let minutes = number("YAGA_JOB_TIMEOUT_MINUTES", "YAGA job timeout")?;
    if !(15..=360).contains(&minutes) {
        return Err("YAGA job timeout must be between 15 and 360 minutes".into());
    }
    let span = Duration::from_secs(minutes as u64 * 60);
    let available = span
        .checked_sub(timeout * 48 + Duration::from_secs(60))
        .filter(|d| !d.is_zero())
        .ok_or("YAGA job timeout leaves no bounded polling window")?;
    let poll = start + available.min(Duration::from_secs(480));
    if Instant::now() >= start + available {
        return Err("YAGA job timeout leaves no bounded polling window".into());
    }
    let mut api = transport(
        timeout,
        budget,
        start + span - Duration::from_secs(15),
        start + span,
    )?;
    let (result, n) = if operation == "invalidate" {
        (
            invalidate(&mut api, &repo, &name, &event, run, attempt, &url)?,
            None,
        )
    } else {
        let ci = required("YAGA_PREREQUISITE_WORKFLOW")?;
        let lifecycle = required("YAGA_LIFECYCLE_WORKFLOW")?;
        let Some(source) = load_source_from_wake(&mut api, &repo, &name, &event, &ci, &lifecycle)?
        else {
            outputs("skip", None)?;
            return Ok((
                0,
                format!("YAGA {operation}: skipped: paired CI and lifecycle are not both complete"),
            ));
        };
        if operation == "authorize"
            && required("YAGA_APPROVAL_MARKER")? != "agent-review-approval:v1"
        {
            return Err("protected approval environment marker is invalid".into());
        }
        let publisher = Publisher {
            repository: &repo,
            source: &source,
            lifecycle_workflow: &lifecycle,
            server_url: origin,
            target_url: &url,
            owner_id: number("YAGA_OWNER_ID", "direct review author ID")?,
        };
        let r = match operation {
            "prepare" => publisher.prepare(
                &mut api,
                event["workflow_run"]["path"]
                    .as_str()
                    .ok_or("publisher wake workflow is invalid")?,
                Some(poll.min(start + Duration::from_secs(120))),
            )?,
            "authorize" => publisher.authorize(&mut api)?,
            "observe" | "request" => publisher.review(&mut api, operation == "request", poll)?,
            _ => publisher.finalize(&mut api)?,
        };
        (
            r,
            (operation == "prepare").then_some(source.pull_request_number),
        )
    };
    outputs(result.route, n)?;
    Ok((
        result.exit_code,
        format!("YAGA {operation}: {}", result.message),
    ))
}
