use std::env;
use std::process::Command;
use std::time::Duration;
use yaga_core::cli_action::prepare;
use yaga_core::git::process::{ProcessError, run_streaming};

pub(super) use yaga_core::cli_action::split_lines;

pub(super) fn run() -> Result<u8, String> {
    let invocation = prepare(|key| env::var(key).ok())?;
    let mut child = Command::new(
        env::current_exe().map_err(|_| "CLI action failed to start the YAGA process")?,
    );
    child
        .args(invocation.arguments)
        .current_dir(invocation.workspace)
        .env_clear();
    child.envs(env::vars_os().filter(|(key, _)| {
        let key = key.to_string_lossy().to_ascii_uppercase();
        !key.starts_with("YAGA_")
            && !key.starts_with("_YAGA_")
            && !key.starts_with("PYTHON")
            && !matches!(key.as_str(), "GITHUB_TOKEN" | "GH_TOKEN" | "COMPLETE")
    }));
    if let Some(token) = invocation.gh_token {
        child.env("GH_TOKEN", token);
    }
    let status = run_streaming(child, Duration::from_secs(1080)).map_err(|error| match error {
        ProcessError::Timeout => "CLI action check exceeded its time limit",
        _ => "CLI action failed to start the YAGA process",
    })?;
    Ok(status.code().and_then(|v| v.try_into().ok()).unwrap_or(2))
}
