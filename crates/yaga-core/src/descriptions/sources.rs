//! Exact file, event and authenticated GitHub CLI sources.
use crate::{
    fs::{display_path, read_regular_prefix, resolve_user_path},
    git::process::{ProcessError, run_bounded},
    json::strict_value,
    text::safe_text,
};
use serde_json::Value;
use std::{path::Path, process::Command, time::Duration};

pub const MAX_BODY: usize = 65536;
pub fn read_file(path: &Path, max: usize) -> Result<Vec<u8>, String> {
    let raw = resolve_user_path(path)
        .and_then(|p| read_regular_prefix(&p, max))
        .map_err(|_| {
            format!(
                "cannot read regular input file {}",
                safe_text(&display_path(path), 300)
            )
        })?;
    if raw.len() > max {
        return Err(format!("input file exceeds the hard {max}-byte limit"));
    }
    Ok(raw)
}
pub fn decode(raw: Vec<u8>, label: &str) -> Result<String, String> {
    if raw.len() > MAX_BODY {
        return Err(format!("{label} exceeds the hard {MAX_BODY}-byte limit"));
    }
    String::from_utf8(raw).map_err(|_| format!("{label} is not valid UTF-8"))
}
pub fn read_event(path: &Path) -> Result<(String, i64), String> {
    let event = strict_value(&read_file(path, 1024 * 1024)?)
        .map_err(|_| "pull request event is invalid or ambiguous UTF-8 JSON")?;
    let event = event
        .as_object()
        .ok_or("pull request event must be an object")?;
    let action = event
        .get("action")
        .and_then(Value::as_str)
        .filter(|s| {
            matches!(
                *s,
                "opened"
                    | "reopened"
                    | "synchronize"
                    | "edited"
                    | "ready_for_review"
                    | "converted_to_draft"
            )
        })
        .ok_or("pull request event action is unsupported")?;
    let pr = event
        .get("pull_request")
        .filter(|p| p.is_object())
        .ok_or("pull request event must contain a pull_request object")?;
    let number = event
        .get("number")
        .and_then(Value::as_i64)
        .filter(|&n| n > 0 && pr["number"].as_i64() == Some(n))
        .ok_or("pull request numbers must be matching bounded positive integers")?;
    if pr["state"] != "open" {
        return Err("pull request state must be open".into());
    }
    let draft = pr["draft"]
        .as_bool()
        .ok_or("pull request draft state must be a boolean")?;
    if (action == "ready_for_review" && draft) || (action == "converted_to_draft" && !draft) {
        return Err("pull request action and draft state are inconsistent".into());
    }
    let body = match &pr["body"] {
        Value::Null => "",
        Value::String(s) => s,
        _ => return Err("pull request body must be a string or null".into()),
    };
    if body.len() > MAX_BODY {
        return Err(format!(
            "pull request body exceeds the hard {MAX_BODY}-byte limit"
        ));
    }
    Ok((body.into(), number))
}

pub use crate::gh::PullRequest;
use crate::gh::{configure, gh_executable};
impl PullRequest {
    pub fn fetch_description(&self) -> Result<(String, String), String> {
        let exe = gh_executable()?;
        let mut command = Command::new(exe);
        command.args([
            "pr",
            "view",
            &self.number().to_string(),
            "--repo",
            &self.repository(),
            "--json",
            "body,number,url",
        ]);
        configure(&mut command);
        let result = run_bounded(
            command,
            None,
            512 * 1024,
            64 * 1024,
            Duration::from_secs(30),
        )
        .map_err(|e| match e {
            ProcessError::Timeout => "gh exceeded the 30-second pull-request description timeout",
            ProcessError::StdoutLimit | ProcessError::StderrLimit => {
                "gh exceeded the bounded pull-request response output limit"
            }
            _ => "gh could not execute its bounded pull-request description request",
        })?;
        if !result.status.success() {
            return Err("gh could not fetch the pull request; check gh authentication, repository access, PR existence, and network connectivity".into());
        }
        self.description_response(&result.stdout)
    }
    fn description_response(&self, bytes: &[u8]) -> Result<(String, String), String> {
        let value =
            strict_value(bytes).map_err(|_| "gh returned invalid or ambiguous UTF-8 JSON")?;
        let doc = value
            .as_object()
            .filter(|d| {
                d.len() == 3 && ["number", "body", "url"].iter().all(|k| d.contains_key(*k))
            })
            .ok_or("gh returned an unexpected pull-request description document")?;
        if doc["number"].as_i64() != Some(self.number()) {
            return Err("gh returned a different or invalid pull-request number".into());
        }
        let actual = doc["url"]
            .as_str()
            .and_then(|s| Self::parse(s, None).ok())
            .ok_or("gh returned an invalid pull-request URL")?;
        if actual.number() != self.number()
            || !actual.repository().eq_ignore_ascii_case(&self.repository())
        {
            return Err("gh returned a different pull-request identity".into());
        }
        let body = match &doc["body"] {
            Value::Null => "",
            Value::String(s) => s,
            _ => return Err("gh returned a non-text pull-request body".into()),
        };
        if body.len() > MAX_BODY {
            return Err("pull-request body exceeds the hard 65536-byte limit".into());
        }
        Ok((body.into(), actual.url()))
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn returned_identity_must_match_both_repo_and_number() {
        let pr = PullRequest::parse("7", Some("owner/repo")).unwrap();
        assert_eq!(
            pr.description_response(
                br#"{"number":7,"body":"text","url":"https://GitHub.com/Owner/Repo/pull/7"}"#
            )
            .unwrap()
            .0,
            "text"
        );
        assert!(
            pr.description_response(
                br#"{"number":7,"body":"","url":"https://github.com/other/repo/pull/7"}"#
            )
            .is_err()
        );
        assert!(pr.description_response(br#"{"number":7,"number":7,"body":"","url":"https://github.com/owner/repo/pull/7"}"#).is_err());
    }
}
