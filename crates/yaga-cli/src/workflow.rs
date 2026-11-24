//! Installed workflow checks share input selection and bounded YAML composition.
use crate::{
    Format,
    trees::{ChildReport, Failure, pretty},
};
use clap::{Args, Subcommand};
use serde_json::json;
use std::{
    path::{Path, PathBuf},
    process::ExitCode,
};
use yaga_core::{
    repository::normalize_security,
    text::{github_data, json_text, safe_text},
    workflows::{self, Diagnostic, ParsedInput, WorkflowInput},
};

#[derive(Args)]
pub struct WorkflowArgs {
    #[command(subcommand)]
    command: Command,
}
#[derive(Args)]
struct Paths {
    /// Workflow files or direct-child directories. Defaults to .github/workflows.
    paths: Vec<PathBuf>,
    #[arg(long, default_value = ".")]
    repo: PathBuf,
    #[arg(long, value_enum, ignore_case = true, default_value = "text")]
    format: Format,
}
#[derive(Subcommand)]
enum Command {
    /// Require immutable external references in selected GitHub workflows.
    Check(Paths),
    /// Enforce a bounded GitHub Actions trust policy.
    Security {
        #[command(flatten)]
        paths: Paths,
        #[arg(long)]
        profile: Option<String>,
        #[arg(long = "rule")]
        rules: Vec<String>,
    },
    /// Lint selected workflows with YAGA's pinned actionlint container.
    Lint(Paths),
}
#[derive(Clone, Copy)]
enum Check<'a> {
    References,
    Security(&'a str, &'a [String]),
    Lint,
}
impl Check<'_> {
    fn kind(&self) -> &str {
        match self {
            Self::References => "github_workflow_policy",
            Self::Security(..) => "github_workflow_security",
            Self::Lint => "github_workflow_lint",
        }
    }
    fn title(&self) -> &str {
        match self {
            Self::References => "workflow policy",
            Self::Security(..) => "workflow security",
            Self::Lint => "workflow lint",
        }
    }
}
struct Row {
    path: String,
    references: usize,
    diagnostics: Vec<Diagnostic>,
}

fn render(check: Check<'_>, rows: Vec<Row>) -> ChildReport {
    let checked = rows.len();
    let passed = rows.iter().filter(|r| r.diagnostics.is_empty()).count();
    let failed = checked - passed;
    let findings: usize = rows.iter().map(|r| r.diagnostics.len()).sum();
    let references: usize = rows.iter().map(|r| r.references).sum();
    let mut document = json!({"schema_version":1,"kind":check.kind()});
    let mut text = Vec::new();
    if let Check::Security(profile, rules) = check {
        document["profile"] = json!(profile);
        document["rules"] = json!(rules);
        text.push(format!("Profile: {profile}; rules: {}.", rules.join(", ")));
    }
    document["valid"] = json!(failed == 0);
    document["checked"] = json!(checked);
    document["passed"] = json!(passed);
    document["failed"] = json!(failed);
    let (count_unit, heading) = if matches!(check, Check::References) {
        document["references_checked"] = json!(references);
        ("reference(s)", "Checked")
    } else {
        document["diagnostics"] = json!(findings);
        (
            "diagnostic(s)",
            if matches!(check, Check::Lint) {
                "Linted"
            } else {
                "Checked"
            },
        )
    };
    let mut documents = Vec::new();
    let mut annotations = Vec::new();
    for row in rows {
        let valid = row.diagnostics.is_empty();
        let status = if valid { "passed" } else { "failed" };
        let mut result = json!({"path":json_text(&row.path,1000),"status":status,"valid":valid});
        let count = if matches!(check, Check::References) {
            result["references_checked"] = json!(row.references);
            row.references
        } else {
            row.diagnostics.len()
        };
        text.push(format!(
            "{:7} {}  {count} {count_unit}",
            status.to_uppercase(),
            safe_text(&row.path, 1000)
        ));
        let mut diagnostics = Vec::new();
        for finding in row.diagnostics {
            diagnostics.push(json!({"code":json_text(&finding.code,100),"message":json_text(&finding.message,500),"line":finding.line,"column":finding.column}));
            text.push(format!(
                "         [{}] line {}, column {}: {}",
                safe_text(&finding.code, 100),
                finding.line,
                finding.column,
                safe_text(&finding.message, 500)
            ));
            if annotations.len() < 50 {
                let path = super::trees::property(&row.path, 1000);
                let title = super::trees::property(&format!("YAGA {}", finding.code), 120);
                annotations.push(format!(
                    "::error file={path},line={},col={},title={title}::{}",
                    finding.line,
                    finding.column,
                    github_data(&finding.message, 500)
                ));
            }
        }
        result["diagnostics"] = json!(diagnostics);
        documents.push(result);
    }
    document["workflows"] = json!(documents);
    let count = if matches!(check, Check::References) {
        references
    } else {
        findings
    };
    text.push(format!("{heading} {checked} workflow file(s): {passed} passed, {failed} failed; {count} {count_unit}."));
    ChildReport {
        document,
        text: text.join("\n"),
        annotations,
    }
}

pub(crate) fn check_parsed(
    inputs: &[ParsedInput],
    security: Option<(&str, &[String])>,
) -> Result<ChildReport, Failure> {
    let mut rows = Vec::new();
    let mut refs = 0;
    let mut count = 0;
    for input in inputs {
        let diagnostics = if let Some((_, rules)) = security {
            input.security_findings(rules).map_err(input_err)?
        } else {
            refs += input.references_checked();
            if refs > 4096 {
                return Err(input_err(
                    "workflow inputs exceed the hard 4096-reference total limit",
                ));
            }
            input.reference_findings()
        };
        count += diagnostics.len();
        if count > 512 {
            return Err(input_err(if security.is_some() {
                "workflow security findings exceed the hard 512-diagnostic total limit"
            } else {
                "workflow inputs exceed the hard 512-diagnostic total limit"
            }));
        }
        rows.push(Row {
            path: input.relative_path().to_owned(),
            references: input.references_checked(),
            diagnostics,
        });
    }
    Ok(render(
        security.map_or(Check::References, |(p, r)| Check::Security(p, r)),
        rows,
    ))
}

pub(crate) fn lint_inputs(repo: &Path, inputs: &[WorkflowInput]) -> Result<ChildReport, Failure> {
    yaga_core::actionlint::install_cli_interrupt_handler().map_err(input_err)?;
    let findings = yaga_core::actionlint::lint_inputs(repo, inputs).map_err(input_err)?;
    Ok(render(
        Check::Lint,
        findings
            .into_iter()
            .map(|(path, diagnostics)| Row {
                path,
                references: 0,
                diagnostics,
            })
            .collect(),
    ))
}

fn input_err(e: impl ToString) -> Failure {
    Failure("input", e.to_string())
}

fn execute(paths: &Paths, check: Check<'_>) -> Result<ChildReport, Failure> {
    let inputs = workflows::load_inputs(&paths.repo, &paths.paths).map_err(input_err)?;
    if matches!(check, Check::Lint) {
        return lint_inputs(&paths.repo, &inputs);
    }
    let parsed = workflows::parse_inputs(&inputs).map_err(input_err)?;
    let security = match check {
        Check::Security(p, r) => Some((p, r)),
        _ => None,
    };
    check_parsed(&parsed, security)
}

fn finish(check: Check<'_>, format: Format, result: Result<ChildReport, Failure>) -> ExitCode {
    let title = check.title();
    match result {
        Err(Failure(kind, detail)) => {
            let message = safe_text(
                &detail,
                if matches!(check, Check::Security(..)) {
                    500
                } else {
                    1000
                },
            );
            let text = format!("YAGA {kind} error: {message}");
            match format {
                Format::Json => eprintln!(
                    "{}",
                    pretty(&json!({"schema_version":1,"error":{"kind":kind,"message":message}}))
                ),
                Format::Github => {
                    eprintln!("::error title=YAGA {title}::{}", github_data(&text, 500))
                }
                Format::Text => eprintln!("{text}"),
            }
            ExitCode::from(2)
        }
        Ok(child) => {
            let valid = child.document["valid"] == true;
            let output = match format {
                Format::Json => pretty(&child.document),
                Format::Text => child.text,
                Format::Github => {
                    let total = child.document["workflows"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|row| row["diagnostics"].as_array().unwrap().len())
                        .sum::<usize>();
                    let mut lines = child.annotations;
                    if total > 50 {
                        lines.truncate(49);
                        let diagnostic = match check {
                            Check::References => "workflow",
                            Check::Security(..) => "workflow security",
                            Check::Lint => "workflow lint",
                        };
                        lines.push(format!("::error title=YAGA {title}::{} additional {diagnostic} diagnostic(s) omitted",total-49));
                    }
                    let summary = child.text.lines().last().unwrap();
                    let (first, rest) = summary.split_once(' ').unwrap();
                    let heading = if matches!(check, Check::Security(..)) {
                        "security checked".into()
                    } else {
                        first.to_lowercase()
                    };
                    lines.push(format!("YAGA {heading} {rest}"));
                    lines.join("\n")
                }
            };
            println!("{output}");
            ExitCode::from(u8::from(!valid))
        }
    }
}

pub fn run(args: WorkflowArgs) -> ExitCode {
    match args.command {
        Command::Check(paths) => finish(
            Check::References,
            paths.format,
            execute(&paths, Check::References),
        ),
        Command::Lint(paths) => finish(Check::Lint, paths.format, execute(&paths, Check::Lint)),
        Command::Security {
            paths,
            profile,
            rules,
        } => match normalize_security(profile.as_deref(), &rules) {
            Ok((p, r)) => {
                let check = Check::Security(&p, &r);
                finish(check, paths.format, execute(&paths, check))
            }
            Err(e) => finish(Check::Security("", &[]), paths.format, Err(input_err(e))),
        },
    }
}
