use super::{BranchDiagnostic, BranchError, CheckedBranch, MAX_BRANCH_NAME_BYTES};
use serde::Serialize;

const MAX_DISPLAY_PATH: usize = 1000;
const MAX_DIAGNOSTIC_MESSAGE: usize = 500;
const TEXT_PATTERN_LIMIT: usize = 8;

/// String operations whose result can depend on the Python Unicode database.
#[derive(Clone, Copy)]
pub(super) enum DisplayField {
    Safe,
    Json,
    Repr,
    Annotation,
}

impl DisplayField {
    pub(super) fn render(self, value: &str, maximum: usize) -> String {
        match self {
            Self::Safe => crate::text::safe_text(value, maximum),
            Self::Json => crate::text::json_text(value, maximum),
            Self::Repr => crate::text::python_repr(value),
            Self::Annotation => crate::text::github_data(value, maximum),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OutputFormat {
    Text,
    Json,
    Github,
}

// Declaration order is the output order of the established schema-v1 JSON.
#[derive(Serialize)]
struct ReportDocument<'a> {
    schema_version: u8,
    kind: &'static str,
    status: &'static str,
    valid: bool,
    policy_path: String,
    branch: String,
    allowed_patterns: Vec<&'a str>,
    matched_pattern: Option<&'a str>,
    diagnostics: Vec<BranchDiagnostic>,
}

#[derive(Serialize)]
struct ErrorDocument<'a> {
    schema_version: u8,
    kind: &'static str,
    status: &'static str,
    valid: bool,
    error: ErrorDetail<'a>,
}

#[derive(Serialize)]
struct ErrorDetail<'a> {
    kind: &'static str,
    message: &'a str,
}

impl CheckedBranch {
    pub fn render(&self, format: OutputFormat) -> String {
        let report = &self.report;
        let path = super::policy::path_text(&self.policy.path);
        let status = if report.valid() { "passed" } else { "failed" };
        let patterns = self.policy.policy.allowed_patterns();
        if format == OutputFormat::Json {
            return serde_json::to_string_pretty(&ReportDocument {
                schema_version: 1,
                kind: "branch_policy",
                status,
                valid: report.valid(),
                policy_path: DisplayField::Json.render(&path, MAX_DISPLAY_PATH),
                branch: DisplayField::Json.render(report.branch(), MAX_BRANCH_NAME_BYTES),
                allowed_patterns: patterns.collect(),
                matched_pattern: report.matched_pattern(),
                diagnostics: report.diagnostic().into_iter().collect(),
            })
            .expect("serializing string fields to a String is infallible");
        }
        let branch = DisplayField::Safe.render(report.branch(), MAX_BRANCH_NAME_BYTES);
        if format == OutputFormat::Github {
            let quoted = DisplayField::Repr.render(&branch, MAX_BRANCH_NAME_BYTES);
            return match &report.outcome {
                Ok(pattern) => format!(
                    "YAGA branch policy: passed; branch {quoted} matched {}.",
                    crate::text::python_repr(pattern)
                ),
                Err(diagnostic) => {
                    let data = DisplayField::Annotation.render(
                        &format!("branch {quoted}: {}", diagnostic.message),
                        MAX_DIAGNOSTIC_MESSAGE,
                    );
                    format!(
                        "::error title=YAGA {}::{data}\nYAGA branch policy: failed for branch {quoted}.",
                        diagnostic.code
                    )
                }
            };
        }
        let result = match &report.outcome {
            Ok(pattern) => format!("Matched: {pattern}"),
            Err(finding) => {
                let omitted = patterns.len().saturating_sub(TEXT_PATTERN_LIMIT);
                let mut preview = patterns
                    .take(TEXT_PATTERN_LIMIT)
                    .collect::<Vec<_>>()
                    .join(", ");
                if omitted > 0 {
                    preview.push_str(&format!(" … (+{omitted} more)"));
                }
                format!(
                    "FAILED  [{}] {}\nAllowed: {preview}",
                    finding.code, finding.message
                )
            }
        };
        format!(
            "Policy: {}\nBranch: {branch}\n{result}\nBranch policy: {status}.",
            DisplayField::Safe.render(&path, MAX_DISPLAY_PATH)
        )
    }
}

impl BranchError {
    pub fn render(&self, format: OutputFormat) -> String {
        let message = DisplayField::Safe.render(&self.to_string(), MAX_DIAGNOSTIC_MESSAGE);
        let kind = self.kind();
        match format {
            OutputFormat::Json => serde_json::to_string_pretty(&ErrorDocument {
                schema_version: 1,
                kind: "branch_policy",
                status: "error",
                valid: false,
                error: ErrorDetail {
                    kind,
                    message: &message,
                },
            })
            .expect("serializing string fields to a String is infallible"),
            OutputFormat::Text => format!("YAGA {kind} error: {message}"),
            OutputFormat::Github => format!(
                "::error title=YAGA branch policy::{}",
                DisplayField::Annotation.render(
                    &format!("YAGA {kind} error: {message}"),
                    MAX_DIAGNOSTIC_MESSAGE
                )
            ),
        }
    }
}
