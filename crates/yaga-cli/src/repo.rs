//! CLI orchestration for explicit repository checks and their independent reports.
mod inspect;
mod reports;
use crate::{Format, commit, commit_report, trees, workflow};
use clap::{Args, Subcommand};
use serde_json::Value;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use yaga_core::git::resolve_repository_tree;
use yaga_core::repository::{
    Provider as P, Selection, load_plan, normalize_providers, normalize_security,
};
use yaga_core::text::{github_data, safe_text};
use yaga_core::workflows::{load_inputs, parse_inputs};

type Failure = (&'static str, String);

#[derive(Args)]
pub struct RepoArgs {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// Run only the explicitly selected providers in canonical order.
    Check(Box<Options>),
    /// Show what an explicit repository plan needs before running checks.
    Inspect(inspect::Options),
}

#[derive(Args)]
struct Options {
    /// Versioned repository check plan. Replaces command-line check selection.
    #[arg(long)]
    plan: Option<PathBuf>,
    /// Provider to run. Repeat explicitly.
    #[arg(long = "check")]
    checks: Vec<String>,
    #[arg(long, default_value = ".")]
    repo: PathBuf,
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long, short = 'c', allow_hyphen_values = true)]
    commit: Option<String>,
    #[arg(long, short = 'r', allow_hyphen_values = true)]
    range: Option<String>,
    #[arg(long, allow_hyphen_values = true)]
    revision: Option<String>,
    #[arg(long = "workflow-path")]
    workflow_paths: Vec<PathBuf>,
    #[arg(long)]
    workflow_security_profile: Option<String>,
    #[arg(long = "workflow-security-rule")]
    workflow_security_rules: Vec<String>,
    #[arg(long)]
    mode_policy: Option<PathBuf>,
    #[arg(long)]
    path_policy: Option<PathBuf>,
    #[arg(long)]
    size_policy: Option<PathBuf>,
    #[arg(long)]
    tree_policy: Option<PathBuf>,
    #[arg(long, value_enum, ignore_case = true, default_value = "text")]
    format: Format,
}

impl Options {
    fn selection(&self) -> Result<Selection, Failure> {
        let policies: BTreeMap<_, _> = [
            (P::Mode, &self.mode_policy),
            (P::Path, &self.path_policy),
            (P::Size, &self.size_policy),
            (P::Tree, &self.tree_policy),
        ]
        .into_iter()
        .filter_map(|(p, v)| v.clone().map(|v| (p, v)))
        .collect();
        let selection = if let Some(path) = &self.plan {
            if !self.checks.is_empty()
                || !self.workflow_paths.is_empty()
                || self.workflow_security_profile.is_some()
                || !self.workflow_security_rules.is_empty()
                || !policies.is_empty()
            {
                return Err(("input","--plan cannot be combined with --check, --workflow-path, --workflow-security-profile, --workflow-security-rule, --mode-policy, --path-policy, --size-policy, or --tree-policy".into()));
            }
            load_plan(path)
                .map_err(|e| ("configuration", e))?
                .bind(&self.repo)
                .map_err(|e| ("input", e))?
        } else {
            Selection {
                providers: normalize_providers(&self.checks).map_err(|e| ("input", e))?,
                workflow_paths: self.workflow_paths.clone(),
                security_profile: self.workflow_security_profile.clone(),
                security_rules: self.workflow_security_rules.clone(),
                policy_paths: policies,
            }
        };
        selection
            .validate(
                self.config.is_some(),
                self.commit.is_some(),
                self.range.is_some(),
                self.revision.is_some(),
            )
            .map_err(|e| ("input", e))?;
        Ok(selection)
    }
}

fn commit_child(options: &Options) -> Result<trees::ChildReport, Failure> {
    let (results, config) = commit::for_repository(
        &options.repo,
        options.config.as_deref(),
        options.commit.as_deref(),
        options.range.as_deref(),
    )?;
    let path = config.path.as_deref();
    let document: Value =
        serde_json::from_str(&commit_report::render(&results, path, Format::Json))
            .expect("commit report is JSON");
    let mut annotations = Vec::new();
    for checked in &results {
        let id = checked
            .sha
            .as_ref()
            .map_or_else(|| checked.source.as_str(), |s| &s[..12]);
        for d in &checked.result.diagnostics {
            if annotations.len() == 50 {
                break;
            }
            let data = github_data(
                &format!(
                    "{}: line {}, column {}: {}",
                    safe_text(id, 80),
                    d.line,
                    d.column,
                    d.message
                ),
                660,
            );
            let severity = match d.severity {
                yaga_core::commits::Severity::Error => "error",
                yaga_core::commits::Severity::Warning => "warning",
            };
            annotations.push(format!("::{severity} title=YAGA {}::{data}", d.code));
        }
        if annotations.len() == 50 {
            break;
        }
    }
    Ok(trees::ChildReport {
        document,
        text: commit_report::render(&results, path, Format::Text),
        annotations,
    })
}

fn input<T>(result: &Result<T, String>) -> Result<&T, Failure> {
    result.as_ref().map_err(|e| ("input", e.clone()))
}

fn tree_provider(p: P) -> trees::Provider {
    match p {
        P::Mode => trees::Provider::Mode,
        P::Path => trees::Provider::Path,
        P::Size => trees::Provider::Size,
        P::Tree => trees::Provider::Tree,
        _ => unreachable!("not a committed-tree provider"),
    }
}

pub fn run(args: RepoArgs) -> ExitCode {
    let options = match args.command {
        Command::Inspect(options) => return inspect::run(options),
        Command::Check(options) => options,
    };
    let selection = match options.selection() {
        Ok(value) => value,
        Err((kind, msg)) => {
            eprintln!("{}", reports::error(kind, &msg, options.format));
            return ExitCode::from(2);
        }
    };
    let mut checks = Vec::new();
    let mut inputs = None;
    let mut parsed = None;
    let mut identity = None;
    for &p in &selection.providers {
        let outcome = (|| match p {
            P::Commit => commit_child(&options),
            P::Workflow | P::WorkflowSecurity | P::WorkflowLint => {
                let files =
                    input(inputs.get_or_insert_with(|| {
                        load_inputs(&options.repo, &selection.workflow_paths)
                    }))?;
                if p == P::WorkflowLint {
                    return workflow::lint_inputs(&options.repo, files).map_err(|e| (e.0, e.1));
                }
                let parsed = input(parsed.get_or_insert_with(|| parse_inputs(files)))?;
                let security = if p == P::WorkflowSecurity {
                    Some(
                        normalize_security(
                            selection.security_profile.as_deref(),
                            &selection.security_rules,
                        )
                        .map_err(|e| ("input", e))?,
                    )
                } else {
                    None
                };
                workflow::check_parsed(
                    parsed,
                    security
                        .as_ref()
                        .map(|(profile, rules)| (profile.as_str(), rules.as_slice())),
                )
                .map_err(|e| (e.0, e.1))
            }
            P::Mode | P::Path | P::Size | P::Tree => {
                let revision = options
                    .revision
                    .as_deref()
                    .expect("validated committed revision");
                let id = identity
                    .get_or_insert_with(|| resolve_repository_tree(&options.repo, revision))
                    .as_ref()
                    .map_err(|e| (e.kind(), e.to_string()))?;
                let path: &Path = &selection.policy_paths[&p];
                trees::check_resolved(tree_provider(p), path, id).map_err(|e| (e.0, e.1))
            }
        })();
        checks.push((p, outcome));
    }
    let (exit, output) = reports::render(&checks, options.format);
    if exit == 2 {
        eprintln!("{output}");
    } else {
        println!("{output}");
    }
    ExitCode::from(exit)
}
