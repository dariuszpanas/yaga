// The installed Python CLI writes through a text stream. Match its native
// Windows newlines even when a consumer redirects a multiline report to a pipe.
#[cfg(windows)]
macro_rules! println {
    ($($arg:tt)*) => { std::print!("{}\r\n", format!($($arg)*).replace('\n', "\r\n")) };
}
#[cfg(windows)]
macro_rules! eprintln {
    ($($arg:tt)*) => { std::eprint!("{}\r\n", format!($($arg)*).replace('\n', "\r\n")) };
}
#[cfg(windows)]
macro_rules! print {
    ($($arg:tt)*) => { std::print!("{}", format!($($arg)*).replace('\n', "\r\n")) };
}

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand, ValueEnum};
use yaga_core::branch::{self, OutputFormat};

mod action;
mod change;
mod commit;
mod commit_report;
mod completion;
mod config;
mod description;
mod gate;
mod github_commit;
mod policy;
mod presentation;
mod repo;
mod review;
mod self_manage;
mod test;
mod trees;
mod triage;
mod workflow;

#[derive(Parser)]
#[command(
    name = "yaga",
    version,
    disable_help_subcommand = true,
    about = "Check repository policies",
    arg_required_else_help = true
)]
struct Cli {
    /// Install completion for the current shell.
    #[arg(long)]
    install_completion: bool,
    /// Show completion for the current shell.
    #[arg(long)]
    show_completion: bool,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Maintain the current installation.
    #[command(name = "self")]
    SelfManage {
        #[command(subcommand)]
        command: self_manage::Operation,
    },
    /// Plan test qualification and evaluate receipts without executing tests.
    Test {
        #[command(subcommand)]
        command: test::Command,
    },
    /// Check pull-request policy against explicitly selected inputs.
    PullRequest {
        #[command(subcommand)]
        command: description::PullRequestCommand,
    },
    /// Check an explicit GitHub event.
    Github {
        #[command(subcommand)]
        command: description::GithubCommand,
    },
    /// Initialize and audit versioned YAGA policies.
    Policy {
        #[command(subcommand)]
        command: policy::Command,
    },
    /// Run explicit repository check providers.
    Repo(repo::RepoArgs),
    /// Check workflow references, security and syntax.
    Workflow(workflow::WorkflowArgs),
    /// Validate provider-neutral review policies and receipts.
    Gate(gate::Gate),
    /// Check changed paths against explicit policy.
    Change(change::ChangeArgs),
    /// Inspect and enforce commit-message policy.
    Commit {
        #[command(subcommand)]
        command: commit::Command,
    },
    /// Initialize or inspect commit policy.
    Config {
        #[command(subcommand)]
        command: config::Command,
    },
    /// Check committed file modes.
    Mode(trees::TreeArgs),
    /// Check committed path portability.
    Path(trees::TreeArgs),
    /// Check committed blob sizes.
    Size(trees::TreeArgs),
    /// Check committed tree layout.
    Tree(trees::TreeArgs),
    /// Check branch-name policy.
    Branch {
        #[command(subcommand)]
        command: BranchCommand,
    },
}

#[derive(Subcommand)]
enum BranchCommand {
    /// Check one explicit short branch name against one explicit policy.
    Check {
        /// Explicit versioned branch-name policy file.
        #[arg(long)]
        policy: PathBuf,
        /// Exact short branch name to check.
        #[arg(long, allow_hyphen_values = true)]
        name: std::ffi::OsString,
        /// Report format.
        #[arg(long, value_enum, default_value = "text", ignore_case = true)]
        format: Format,
        /// Suppress policy reports and use only the exit code.
        #[arg(short, long)]
        quiet: bool,
    },
}

#[derive(Clone, Copy, ValueEnum)]
enum Format {
    Text,
    Json,
    Github,
}

fn main() -> ExitCode {
    let mut args = std::env::args_os().skip(1).peekable();
    let mut first = args.next();
    if first.as_deref() == Some(std::ffi::OsStr::new("__action")) {
        return action::run();
    }
    if let Some(code) = completion::runtime() {
        return code;
    }
    // Typer accepts a read-only script request after a root separator. Only
    // promote that exact flag so later arguments cannot select installation.
    if first.as_deref() == Some(std::ffi::OsStr::new("--"))
        && args.peek().is_some_and(|s| s == "--show-completion")
    {
        first = args.next();
    }
    if let Some(show) = completion::eager_setup(first.as_deref(), args) {
        return completion::run(show);
    }
    let cli = match presentation::parse() {
        Ok(cli) => cli,
        Err(code) => return code,
    };
    if cli.install_completion || cli.show_completion {
        return completion::run(!cli.install_completion);
    }
    let Some(command) = cli.command else {
        return ExitCode::from(2);
    };
    let (policy, name, format, quiet) = match command {
        Command::SelfManage { command } => return self_manage::run(command),
        Command::Test { command } => return test::run(command),
        Command::PullRequest { command } => return description::local(command),
        Command::Github { command } => return description::github(command),
        Command::Repo(args) => return repo::run(args),
        Command::Gate(args) => return gate::run(args),
        Command::Policy { command } => return policy::run(command),
        Command::Branch {
            command:
                BranchCommand::Check {
                    policy,
                    name,
                    format,
                    quiet,
                },
        } => (policy, name, format, quiet),
        Command::Commit { command } => return commit::run(command),
        Command::Config { command } => return config::run(command),
        Command::Workflow(args) => return workflow::run(args),
        Command::Change(args) => return change::run(args),
        Command::Mode(args) => return trees::run(trees::Provider::Mode, args),
        Command::Path(args) => return trees::run(trees::Provider::Path, args),
        Command::Size(args) => return trees::run(trees::Provider::Size, args),
        Command::Tree(args) => return trees::run(trees::Provider::Tree, args),
    };
    let format = match format {
        Format::Text => OutputFormat::Text,
        Format::Json => OutputFormat::Json,
        Format::Github => OutputFormat::Github,
    };
    let checked = match name.to_str() {
        Some(value) => branch::check_branch(&policy, value),
        // As in the Python API, a bad policy takes precedence over a bad name.
        None => branch::load_branch_policy(&policy).and_then(|_| {
            Err(branch::BranchError::Input(
                "branch name must be valid UTF-8".into(),
            ))
        }),
    };
    match checked {
        Ok(checked) => {
            if !quiet {
                println!("{}", checked.render(format));
            }
            ExitCode::from(u8::from(!checked.report.valid()))
        }
        Err(error) => {
            eprintln!("{}", error.render(format));
            ExitCode::from(2)
        }
    }
}
