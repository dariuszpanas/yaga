use std::collections::HashMap;
use std::path::Path;

use serde::Serialize;
use yaga_core::commits::Severity;
use yaga_core::github_commit::{self, PullRequestEvent};
use yaga_core::text::{json_text, safe_text};

use crate::Format;

pub use yaga_core::commit_source::Checked;

#[derive(Serialize)]
struct Finding<'a> {
    code: &'a str,
    severity: Severity,
    message: String,
    line: usize,
    column: usize,
}

#[derive(Serialize)]
struct Commit<'a> {
    source: String,
    sha: Option<&'a str>,
    header: String,
    status: &'static str,
    valid: bool,
    skipped_reason: Option<&'a str>,
    diagnostics: Vec<Finding<'a>>,
}

#[derive(Serialize)]
struct Report<'a> {
    schema_version: u8,
    warning_count: usize,
    valid: bool,
    checked: usize,
    passed: usize,
    failed: usize,
    skipped: usize,
    config_path: Option<String>,
    commits: Vec<Commit<'a>>,
}

pub fn render(results: &[Checked], config: Option<&Path>, format: Format) -> String {
    render_results(results, config, format, None)
}

pub fn render_pull_request(report: &github_commit::Report, format: Format) -> String {
    render_results(
        &report.results,
        report.config_path.as_deref(),
        format,
        Some((&report.event, report.has_proposed_message)),
    )
}

fn render_results(
    results: &[Checked],
    config: Option<&Path>,
    format: Format,
    pull_request: Option<(&PullRequestEvent, bool)>,
) -> String {
    let commits = results
        .iter()
        .map(|checked| {
            let result = &checked.result;
            let valid = result.valid();
            let status = if result.skipped_reason.is_some() {
                "skipped"
            } else if valid {
                "passed"
            } else {
                "failed"
            };
            Commit {
                source: json_text(&checked.source, 1000),
                sha: checked.sha.as_deref(),
                header: json_text(&result.header, 160),
                status,
                valid,
                skipped_reason: result.skipped_reason,
                diagnostics: result
                    .diagnostics
                    .iter()
                    .map(|d| Finding {
                        code: d.code,
                        severity: d.severity,
                        message: json_text(&d.message, 500),
                        line: d.line,
                        column: d.column,
                    })
                    .collect(),
            }
        })
        .collect::<Vec<_>>();
    let failed = commits.iter().filter(|c| !c.valid).count();
    let skipped = commits
        .iter()
        .filter(|c| c.skipped_reason.is_some())
        .count();
    let checked = commits.len();
    let report = Report {
        schema_version: 1,
        warning_count: results
            .iter()
            .flat_map(|r| &r.result.diagnostics)
            .filter(|d| d.severity == Severity::Warning)
            .count(),
        valid: failed == 0,
        checked,
        passed: checked - failed - skipped,
        failed,
        skipped,
        config_path: config.map(|p| json_text(&p.to_string_lossy(), 1000)),
        commits,
    };
    if matches!(format, Format::Json) {
        if let Some((event, proposed)) = pull_request {
            return pull_request_json(&report, event, proposed);
        }
        return serde_json::to_string_pretty(&report).unwrap();
    }
    let github = matches!(format, Format::Github);
    let mut lines = Vec::new();
    let mut annotations = Vec::new();
    let mut counts = HashMap::new();
    let mut codes = Vec::new();
    if !github && let Some((event, _)) = pull_request {
        lines.push(format!(
            "Pull request #{}: {}..{}",
            event.number,
            &event.base_sha[..12],
            &event.head_sha[..12]
        ));
    }
    for (index, (checked, commit)) in results.iter().zip(&report.commits).enumerate() {
        let label = checked
            .sha
            .as_ref()
            .map_or(checked.source.as_str(), |sha| &sha[..12]);
        if !github {
            let kind = if let Some((_, proposed)) = pull_request {
                format!(
                    "{:6} ",
                    if index == 0 {
                        "TITLE"
                    } else if index == 1 && proposed {
                        "MESSAGE"
                    } else {
                        "COMMIT"
                    }
                )
            } else {
                String::new()
            };
            lines.push(format!(
                "{kind}{:7} {}  {}",
                commit.status.to_uppercase(),
                safe_text(label, 80),
                safe_text(&checked.result.header, 160)
            ));
            if let Some(reason) = commit.skipped_reason {
                let indent = if pull_request.is_some() { 15 } else { 9 };
                lines.push(format!("{:indent$}skipped: {reason}", ""));
            }
        }
        for d in &checked.result.diagnostics {
            if github {
                if !counts.contains_key(d.code) {
                    codes.push(d.code);
                }
                *counts.entry(d.code).or_insert(0usize) += 1;
                let message = safe_text(
                    &format!(
                        "{label}: [{}] line {}, column {}: {}",
                        d.code, d.line, d.column, d.message
                    ),
                    660,
                );
                annotations.push((d.severity, message));
            } else {
                let warning = if d.severity == Severity::Warning {
                    "[warning] "
                } else {
                    ""
                };
                let indent = if pull_request.is_some() { 15 } else { 9 };
                lines.push(format!(
                    "{:indent$}{warning}[{}] line {}, column {}: {}",
                    "",
                    d.code,
                    d.line,
                    d.column,
                    safe_text(&d.message, 500)
                ));
            }
        }
    }
    if annotations.len() > 50 {
        let tail = &annotations[49..];
        let severity = if tail.iter().any(|(s, _)| *s == Severity::Error) {
            Severity::Error
        } else {
            Severity::Warning
        };
        let omitted = format!("{} additional policy diagnostic(s) omitted", tail.len());
        annotations.truncate(49);
        annotations.push((severity, omitted));
    }
    for (severity, message) in annotations {
        let level = if severity == Severity::Warning {
            "warning"
        } else {
            "error"
        };
        lines.push(format!(
            "::{level} title=YAGA commit policy::{}",
            message.replace('%', "%25")
        ));
    }
    let prefix = if github { "YAGA checked" } else { "Checked" };
    if let Some((event, proposed)) = pull_request {
        let commit_count = report.checked - 1 - usize::from(proposed);
        let message = if proposed {
            ", one proposed message"
        } else {
            ""
        };
        let mut summary = if github {
            format!(
                "YAGA checked pull request #{}: one title{message} and {commit_count} commit(s); {failed} failed, {skipped} skipped.",
                event.number
            )
        } else {
            format!(
                "Checked one title{message} and {commit_count} commit(s): {} passed, {failed} failed, {skipped} skipped.",
                report.passed
            )
        };
        if github
            && results
                .iter()
                .any(|r| r.result.skipped_reason == Some(github_commit::DEPENDABOT_REASON))
        {
            summary.push_str(" Skip reason: Dependabot pull request.")
        }
        lines.push(summary)
    } else {
        lines.push(format!(
            "{prefix} {} commit(s): {} passed, {} failed, {} skipped.",
            report.checked, report.passed, failed, skipped
        ));
    }
    if report.warning_count > 0 {
        lines.push(format!("Warnings: {} (nonblocking).", report.warning_count));
    }
    if github && !codes.is_empty() {
        codes.sort_by_key(|code| std::cmp::Reverse(counts[code]));
        let mut summary: Vec<_> = codes
            .iter()
            .take(8)
            .map(|code| format!("{code} ({})", counts[code]))
            .collect();
        if codes.len() > 8 {
            summary.push(format!("{} additional rule(s) omitted", codes.len() - 8));
        }
        lines.push(format!("Findings by rule: {}", summary.join(", ")));
    }
    if !github && let Some(path) = config {
        lines.push(format!(
            "Config: {}",
            safe_text(&path.to_string_lossy(), 1000)
        ));
    }
    lines.join("\n")
}

fn pull_request_json(report: &Report<'_>, event: &PullRequestEvent, proposed: bool) -> String {
    use serde_json::json;
    let mut selection = json!({
        "action":event.action,"number":event.number,"base_sha":event.base_sha,"head_sha":event.head_sha,
        "base_ref":event.base_ref,"head_ref":event.head_ref,"repository":event.repository,"repository_id":event.repository_id,
        "draft":event.draft,"author":{"login":event.author_login,"id":event.author_id,"type":event.author_type},
        "title":report.commits[0],"commits":&report.commits[1+usize::from(proposed)..],
    });
    if proposed {
        selection["proposed_message"] = json!(report.commits[1]);
    }
    serde_json::to_string_pretty(&json!({
        "schema_version":report.schema_version,"warning_count":report.warning_count,"kind":"pull_request_commit_policy",
        "valid":report.valid,"checked":report.checked,"passed":report.passed,"failed":report.failed,"skipped":report.skipped,
        "config_path":report.config_path,"pull_request":selection,
    })).expect("validated pull request commit report is serializable")
}
