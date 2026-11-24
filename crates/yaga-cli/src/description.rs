use crate::Format;
use clap::{Args, Subcommand};
use serde_json::json;
use std::{
    io::{self, Read},
    path::PathBuf,
    process::ExitCode,
};
use yaga_core::{
    commits::{Severity, TyposPolicy},
    descriptions::{
        CheckError, Report, check_description,
        policy::load_description_policy,
        sources::{self, MAX_BODY, PullRequest},
    },
    text::safe_text,
};

#[derive(Subcommand)]
pub enum PullRequestCommand {
    Review {
        #[command(subcommand)]
        command: crate::review::Command,
    },
    Triage {
        #[command(subcommand)]
        command: crate::triage::Command,
    },
    Description {
        #[command(subcommand)]
        command: Local,
    },
}
#[derive(Subcommand)]
pub enum GithubCommand {
    PullRequest {
        #[command(subcommand)]
        command: GithubPullRequest,
    },
}
#[derive(Subcommand)]
pub enum GithubPullRequest {
    /// Check the PR title and exact event base-to-head commit range.
    Check(crate::github_commit::Options),
    Description {
        #[command(subcommand)]
        command: GithubDescription,
    },
}
#[derive(Subcommand)]
pub enum Local {
    Check {
        #[command(flatten)]
        common: Common,
        #[arg(long)]
        file: Option<PathBuf>,
        #[arg(long)]
        stdin: bool,
        #[arg(long)]
        pr: Option<String>,
        #[arg(long)]
        repo: Option<String>,
    },
}
#[derive(Subcommand)]
pub enum GithubDescription {
    Check {
        #[command(flatten)]
        common: Common,
        #[arg(long)]
        event_file: PathBuf,
    },
}
#[derive(Args)]
pub struct Common {
    #[arg(long)]
    policy: PathBuf,
    #[arg(long)]
    template: Option<PathBuf>,
    #[arg(long, value_enum, default_value = "text", ignore_case = true)]
    format: Format,
    #[arg(short, long)]
    quiet: bool,
}
enum Source {
    File(PathBuf),
    Event(PathBuf),
    Stdin,
    Pr(PullRequest),
}
struct Checked {
    report: Report,
    policy_path: PathBuf,
    source: String,
    number: Option<i64>,
}
fn file(path: &std::path::Path) -> Result<String, String> {
    sources::decode(sources::read_file(path, MAX_BODY)?, "description file")
}
fn select(
    file: Option<PathBuf>,
    stdin: bool,
    pr: Option<String>,
    repo: Option<String>,
) -> Result<Source, String> {
    if usize::from(file.is_some()) + usize::from(stdin) + usize::from(pr.is_some()) != 1 {
        return Err(
            "select exactly one description source: --file, --stdin, --pr, or --event-file".into(),
        );
    }
    if let Some(value) = pr {
        return PullRequest::parse(&value, repo.as_deref()).map(Source::Pr);
    }
    if repo.is_some() {
        return Err("--repo requires a numeric --pr source".into());
    }
    Ok(file.map_or(Source::Stdin, Source::File))
}
fn check(common: &Common, source: Source) -> Result<Checked, CheckError> {
    let input = |s| ("input", s);
    let (policy, policy_path) =
        load_description_policy(&common.policy).map_err(|s| ("configuration", s))?;
    let template = common
        .template
        .as_deref()
        .map(file)
        .transpose()
        .map_err(input)?;
    let (body, source, number) = match source {
        Source::File(p) => (
            file(&p).map_err(input)?,
            yaga_core::fs::display_path(&p),
            None,
        ),
        Source::Event(p) => {
            let (body, n) = sources::read_event(&p).map_err(input)?;
            (body, yaga_core::fs::display_path(&p), Some(n))
        }
        Source::Pr(pr) => {
            let (body, url) = pr.fetch_description().map_err(input)?;
            (body, url, Some(pr.number()))
        }
        Source::Stdin => {
            let mut raw = vec![];
            io::stdin()
                .lock()
                .take((MAX_BODY + 1) as u64)
                .read_to_end(&mut raw)
                .map_err(|_| input("standard input could not be read as UTF-8".into()))?;
            (
                sources::decode(raw, "standard input").map_err(input)?,
                "stdin".into(),
                None,
            )
        }
    };
    let mut report = check_description(&body, &policy, template.as_deref())?;
    if policy.typos == TyposPolicy::Check {
        let findings = yaga_core::typos::check(
            std::path::Path::new("."),
            &report.spelling,
            true,
            Severity::Error,
        )
        .map_err(input)?;
        let lengths: Vec<_> = report.spelling.split('\n').map(str::len).collect();
        for d in findings {
            if lengths
                .get(d.line - 1)
                .is_none_or(|&length| d.column > length + 1)
            {
                return Err(input(
                    "typos returned a location outside the description".into(),
                ));
            }
            report.add("description.typo", d.message, d.line, d.column);
        }
    }
    Ok(Checked {
        report,
        policy_path,
        source,
        number,
    })
}
fn render(c: &Checked, common: &Common) -> String {
    let report = &c.report;
    let passed = report.diagnostic_count == 0;
    let status = if passed { "passed" } else { "failed" };
    let source = safe_text(&c.source, 1000);
    if matches!(common.format, Format::Json) {
        let findings:Vec<_>=report.diagnostics.iter().map(|d|json!({"code":d.code,"message":safe_text(&d.message,500),"line":d.line,"column":d.column})).collect();
        return serde_json::to_string_pretty(&json!({"schema_version":1,"kind":"pull_request_description","status":status,"valid":passed,
            "policy_path":safe_text(&c.policy_path.to_string_lossy(),1000),"source":source,"template_path":common.template.as_ref().map(|p|safe_text(&yaga_core::fs::display_path(p),1000)),
            "pull_request_number":c.number,"word_count":report.word_count,"character_count":report.character_count,"diagnostic_count":report.diagnostic_count,
            "omitted_diagnostic_count":report.diagnostic_count-findings.len(),"diagnostics":findings})).unwrap();
    }
    let github = matches!(common.format, Format::Github);
    yaga_core::descriptions::render_findings(
        &source,
        report.word_count,
        report.diagnostic_count,
        report
            .diagnostics
            .iter()
            .map(|d| (d.code, d.line, safe_text(&d.message, 500))),
        github,
    )
}
fn run(common: Common, source: Result<Source, String>) -> ExitCode {
    match source
        .map_err(|e| ("input", e))
        .and_then(|s| check(&common, s))
    {
        Ok(c) => {
            if !common.quiet {
                println!("{}", render(&c, &common));
            }
            ExitCode::from(u8::from(c.report.diagnostic_count != 0))
        }
        Err((kind, msg)) => {
            let message = safe_text(&msg, 500);
            let text=match common.format{
                Format::Json=>serde_json::to_string_pretty(&json!({"schema_version":1,"kind":"pull_request_description","status":"error","valid":false,"error":{"kind":kind,"message":message}})).unwrap(),
                Format::Github=>format!("::error title=YAGA PR description::{}",safe_text(&format!("{kind} error: {message}"),500).replace('%',"%25")),
                Format::Text=>format!("YAGA {kind} error: {message}"),
            };
            eprintln!("{text}");
            ExitCode::from(2)
        }
    }
}
pub fn local(command: PullRequestCommand) -> ExitCode {
    match command {
        PullRequestCommand::Triage { command } => crate::triage::run(command),
        PullRequestCommand::Review { command } => crate::review::run(command),
        PullRequestCommand::Description {
            command:
                Local::Check {
                    common,
                    file,
                    stdin,
                    pr,
                    repo,
                },
        } => run(common, select(file, stdin, pr, repo)),
    }
}
pub fn github(command: GithubCommand) -> ExitCode {
    let GithubCommand::PullRequest { command } = command;
    match command {
        GithubPullRequest::Check(options) => crate::github_commit::run(options),
        GithubPullRequest::Description {
            command: GithubDescription::Check { common, event_file },
        } => run(common, Ok(Source::Event(event_file))),
    }
}
