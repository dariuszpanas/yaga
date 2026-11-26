//! Bounded Typos adapter for installed commit checks.

use std::{
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

use crate::commits::{Diagnostic, MAX_MESSAGE_BYTES, Severity, normalize_message};
use crate::git::process::{ProcessError, run_bounded};
use crate::text::python_repr;
use serde::Deserialize;
use serde_json::Value;

const MAX_OUTPUT: usize = 64 * 1024;

#[derive(Deserialize)]
struct Typo {
    #[serde(rename = "type")]
    kind: String,
    line_num: usize,
    byte_offset: Option<usize>,
    typo: String,
    corrections: Vec<String>,
}

fn installed_typos(directory: &Path) -> Result<PathBuf, String> {
    crate::git::external_executable(directory, "typos").map_err(|err| {
        let message = err.to_string();
        if message == "typos executable was not found" {
            "commit policy requires the typos executable, but it is not installed".into()
        } else {
            message
        }
    })
}

pub fn check(
    directory: &Path,
    message: &str,
    isolated: bool,
    severity: Severity,
) -> Result<Vec<Diagnostic>, String> {
    let executable = installed_typos(directory)?;
    let message = normalize_message(message);
    if message.len() > MAX_MESSAGE_BYTES {
        return Err("commit message exceeds the hard byte limit for Typos input".into());
    }
    let mut command = Command::new(executable);
    command
        .current_dir(directory)
        .args(["-", "--format", "json"]);
    if isolated {
        command.arg("--isolated");
    }
    let result = run_bounded(
        command,
        Some(message.into_bytes()),
        MAX_OUTPUT,
        MAX_OUTPUT,
        Duration::from_secs(5),
    )
    .map_err(|err| match err {
        ProcessError::Timeout => "typos did not complete within its bounded commit-message check",
        ProcessError::StdoutLimit | ProcessError::StderrLimit => {
            "typos exceeded its bounded commit-message output limit"
        }
        _ => "typos could not complete its bounded commit-message check",
    })?;
    match result.status.code() {
        Some(0 | 2) if !result.stderr.is_empty() => {
            Err("typos returned unexpected diagnostic output on stderr".into())
        }
        Some(0) if result.stdout.is_empty() => Ok(vec![]),
        Some(0) => Err("typos returned unexpected output for a successful spelling check".into()),
        Some(2) => parse(&result.stdout, severity),
        _ => Err(format!(
            "typos failed while checking the commit message: {}",
            crate::text::safe_text(&String::from_utf8_lossy(&result.stderr), 500)
        )),
    }
}

fn parse(data: &[u8], severity: Severity) -> Result<Vec<Diagnostic>, String> {
    let text = std::str::from_utf8(data).map_err(|_| "typos returned non-UTF-8 JSON output")?;
    let mut diagnostics = Vec::new();
    for line in text.lines() {
        let value = crate::json::strict_value(line.as_bytes())
            .map_err(|_| "typos returned malformed JSON output")?;
        if value.get("type").and_then(Value::as_str) != Some("typo") {
            return Err("typos returned an unexpected JSON diagnostic".into());
        }
        let typo: Typo = serde_json::from_value(value)
            .map_err(|_| "typos returned a malformed JSON diagnostic")?;
        if typo.kind != "typo"
            || !(1..=100_000).contains(&typo.line_num)
            || typo.byte_offset.is_some_and(|v| v > 100_000)
            || !(1..=128).contains(&typo.typo.len())
            || typo.corrections.len() > 16
            || typo
                .corrections
                .iter()
                .any(|s| !(1..=128).contains(&s.len()))
        {
            return Err("typos returned a malformed JSON diagnostic".into());
        }
        let mut message = format!("possible typo {}", python_repr(&typo.typo));
        if !typo.corrections.is_empty() {
            let suggestion = typo.corrections[..typo.corrections.len().min(8)].join(", ");
            message.push_str("; suggestions: ");
            message
                .push_str(&suggestion[..suggestion.floor_char_boundary(256.min(suggestion.len()))]);
        }
        diagnostics.push(Diagnostic {
            code: "typos.word",
            severity,
            message,
            line: typo.line_num,
            column: typo.byte_offset.unwrap_or(0) + 1,
        });
        if diagnostics.len() >= 256 {
            return Err("typos returned too many commit-message diagnostics".into());
        }
    }
    if diagnostics.is_empty() {
        return Err("typos reported findings without JSON diagnostics".into());
    }
    Ok(diagnostics)
}
