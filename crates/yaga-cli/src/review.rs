//! Installed pull-request review CLI.
use crate::Format;
use clap::{Args, Subcommand, ValueEnum};
use serde_json::json;
use std::{path::PathBuf, process::ExitCode};
use yaga_core::{
    agent_review::{self, Outcome, Policy},
    fs::{read_regular_prefix, resolve_user_path},
    gh::PullRequest,
    review::{self, Checked, Error, Snapshot, Submit},
    text::{github_data, safe_text},
};

#[derive(Subcommand)]
pub enum Command {
    Check(Options),
}
#[derive(Args)]
pub struct Options {
    #[arg(long)]
    policy: PathBuf,
    #[arg(long)]
    file: Option<PathBuf>,
    #[arg(long)]
    pr: Option<String>,
    #[arg(long)]
    repo: Option<String>,
    #[arg(long, allow_hyphen_values = true)]
    wait_seconds: Option<i64>,
    #[arg(long, value_enum, default_value = "none", ignore_case = true)]
    submit_review: Submission,
    #[arg(long)]
    triage_policy: Option<PathBuf>,
    #[arg(long, value_enum, default_value = "text", ignore_case = true)]
    format: Format,
    #[arg(short, long)]
    quiet: bool,
}
#[derive(Clone, Copy, ValueEnum)]
enum Submission {
    None,
    Approve,
    RequestChanges,
    Both,
}
impl From<Submission> for Submit {
    fn from(value: Submission) -> Self {
        match value {
            Submission::None => Self::None,
            Submission::Approve => Self::Approve,
            Submission::RequestChanges => Self::RequestChanges,
            Submission::Both => Self::Both,
        }
    }
}
fn execute(options: &Options) -> Result<(Checked, String, String), Error> {
    let input = |s: &str| Error::Input(s.into());
    if options.file.is_some() == options.pr.is_some() {
        return Err(input(
            "select exactly one pull-request review source: --file or --pr",
        ));
    }
    if options.repo.is_some() && options.pr.is_none() {
        return Err(input("--repo requires a numeric --pr source"));
    }
    if options.wait_seconds.is_some() && options.pr.is_none() {
        return Err(input("--wait-seconds requires a live --pr source"));
    }
    if options
        .wait_seconds
        .is_some_and(|n| !(0..=600).contains(&n))
    {
        return Err(input("--wait-seconds must be an integer between 0 and 600"));
    }
    let submit = Submit::from(options.submit_review);
    if submit != Submit::None && options.pr.is_none() {
        return Err(input("--submit-review requires a live --pr source"));
    }
    if matches!(submit, Submit::Approve | Submit::Both) && options.triage_policy.is_none() {
        return Err(input(
            "APPROVE submission requires an explicit trusted --triage-policy",
        ));
    }
    if options.triage_policy.is_some() && options.pr.is_none() {
        return Err(input("--triage-policy requires a live --pr source"));
    }
    if options.triage_policy.is_some() && submit == Submit::None {
        return Err(input("--triage-policy requires explicit --submit-review"));
    }
    let reference = options
        .pr
        .as_deref()
        .map(|s| PullRequest::parse(s, options.repo.as_deref()))
        .transpose()?;
    let path = resolve_user_path(&options.policy)
        .map_err(|e| Error::Configuration(format!("cannot resolve Agent review policy: {e}")))?;
    let bytes = read_regular_prefix(&path, agent_review::MAX_BYTES).map_err(|e| {
        Error::Configuration(format!(
            "cannot read Agent review policy {}: {e}",
            path.display()
        ))
    })?;
    let policy = Policy::parse(&bytes, &path).map_err(Error::Configuration)?;
    let triage = options
        .triage_policy
        .as_deref()
        .map(yaga_core::triage::load_policy)
        .transpose()
        .map_err(Error::Configuration)?;
    let (checked, source) = if let Some(reference) = reference {
        let url = reference.url();
        let checked = review::check_live(
            &policy,
            reference,
            options.wait_seconds.map(|n| n as u64),
            submit,
            triage.as_ref(),
        )?;
        let source = checked.snapshot.url().unwrap_or(&url).to_owned();
        (checked, source)
    } else {
        let path = resolve_user_path(options.file.as_ref().unwrap()).map_err(|e| {
            Error::Input(format!("cannot resolve pull-request review snapshot: {e}"))
        })?;
        (
            Checked::offline(&policy, Snapshot::load(&path)?)?,
            path.display().to_string(),
        )
    };
    Ok((
        checked,
        safe_text(&path.display().to_string(), 1000),
        safe_text(&source, 1000),
    ))
}
fn render(checked: &Checked, policy: &str, source: &str, format: Format) -> String {
    let snapshot = &checked.snapshot;
    let aggregate = &checked.aggregate;
    let head = &snapshot.head_sha()[..12];
    if matches!(format, Format::Json) {
        let status = if aggregate.state == Outcome::Passed && !checked.valid() {
            "pending"
        } else {
            aggregate.state.as_str()
        };
        let results: Vec<_> = aggregate.results.iter().map(|r| {
            json!({"lens": r.lens, "outcome": r.outcome, "summary": r.summary.as_deref().map(|s| safe_text(s, 500))})
        }).collect();
        let mut doc = json!({
            "schema_version": 1,
            "kind": "pull_request_review",
            "status": status,
            "valid": checked.valid(),
            "policy_path": policy,
            "source": source,
            "pull_request_number": snapshot.number(),
            "head_sha": snapshot.head_sha(),
            "base_sha": snapshot.base_sha(),
            "plan_digest": aggregate.plan_digest,
            "blocking_failures": aggregate.blocking_failures,
            "blocking_pending": aggregate.blocking_pending,
            "advisory_failures": aggregate.advisory_failures,
            "advisory_pending": aggregate.advisory_pending,
            "results": results,
        });
        if let Some(review) = &checked.submitted {
            doc["submitted_review"] = review.clone()
        }
        if let Some(admission) = &checked.admission {
            doc["triage"] = admission.clone()
        }
        return crate::trees::pretty(&doc);
    }
    let github = matches!(format, Format::Github);
    let mut lines = Vec::new();
    if !github {
        lines.extend([
            format!("Source: {source}"),
            format!("Pull request: #{} (head {head})", snapshot.number()),
        ])
    }
    lines.push(
        crate::gate::render_result(aggregate, if github { "github" } else { "text" })
            .expect("known output format"),
    );
    if let Some(triage) = &checked.admission {
        let decision = triage["admission"]["decision"].as_str().unwrap();
        lines.push(if github {
            format!("::notice title=YAGA maintainer admission::{decision}")
        } else {
            format!("Maintainer admission: {decision}")
        });
    }
    if let Some(review) = &checked.submitted {
        let state = review["state"].as_str().unwrap();
        let action = review["action"].as_str().unwrap();
        let message = if github {
            format!(
                "::notice title=YAGA pull-request review::Submitted review {state} ({action}, id {}) on head {head}.",
                review["review_id"]
            )
        } else {
            format!("Submitted review: {state} ({action}) on head {head}")
        };
        lines.push(message);
    }
    lines.join("\n")
}
pub fn run(Command::Check(options): Command) -> ExitCode {
    match execute(&options) {
        Ok((checked, policy, source)) => {
            if !options.quiet {
                println!("{}", render(&checked, &policy, &source, options.format))
            }
            ExitCode::from(u8::from(!checked.valid()))
        }
        Err(error) => {
            let kind = error.kind();
            let message = safe_text(&error.to_string(), 500);
            let output = match options.format {
                Format::Json => crate::trees::pretty(
                    &json!({"schema_version":1,"kind":"pull_request_review","status":"error","valid":false,"error":{"kind":kind,"message":message}}),
                ),
                Format::Text => format!("YAGA {kind} error: {message}"),
                Format::Github => format!(
                    "::error title=YAGA PR review::{}",
                    github_data(&format!("{kind} error: {message}"), 2000)
                ),
            };
            eprintln!("{output}");
            ExitCode::from(2)
        }
    }
}
