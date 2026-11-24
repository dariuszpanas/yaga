use std::fs::File;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, Subcommand};
use serde_json::json;
use yaga_core::commit_source::{self, CommitTarget};
use yaga_core::commits::{self, LoadedConfig, MAX_MESSAGE_BYTES};
use yaga_core::fs::resolve_user_path;
use yaga_core::git::Repository;
use yaga_core::text::safe_text;

mod quality;

use crate::{
    Format,
    commit_report::{self, Checked},
    config,
};

#[derive(Subcommand)]
pub enum Command {
    /// Check HEAD or exactly one explicitly selected message source.
    Check(Options),
    /// Request advisory model feedback for the selected commit messages.
    Quality(Box<quality::Options>),
    /// Print a policy-guided editor template.
    Template(config::Template),
}

#[derive(Args)]
pub struct Options {
    #[arg(long)]
    edit: Option<PathBuf>,
    #[arg(long)]
    title: Option<String>,
    #[command(flatten)]
    common: Common,
}

#[derive(Args)]
pub(super) struct Common {
    #[arg(short, long)]
    message: Option<String>,
    #[arg(short = 'F', long)]
    file: Option<PathBuf>,
    #[arg(long)]
    stdin: bool,
    #[arg(short, long, allow_hyphen_values = true)]
    commit: Option<String>,
    #[arg(short, long, allow_hyphen_values = true)]
    range: Option<String>,
    #[arg(long, default_value = ".")]
    repo: PathBuf,
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long, value_enum, default_value = "text", ignore_case = true)]
    format: Format,
    #[arg(short, long)]
    quiet: bool,
}

type Failure = (&'static str, String);

pub(crate) fn for_repository(
    directory: &Path,
    config: Option<&Path>,
    commit: Option<&str>,
    range: Option<&str>,
) -> Result<(Vec<Checked>, LoadedConfig), Failure> {
    check(&Options {
        edit: None,
        title: None,
        common: Common {
            message: None,
            file: None,
            stdin: false,
            commit: commit.map(str::to_owned),
            range: range.map(str::to_owned),
            repo: directory.to_owned(),
            config: config.map(Path::to_owned),
            format: Format::Json,
            quiet: false,
        },
    })
}

fn bounded(stream: impl Read, label: &str) -> Result<String, String> {
    let mut bytes = Vec::new();
    stream
        .take(MAX_MESSAGE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| format!("{label} could not be read as UTF-8"))?;
    if bytes.len() > MAX_MESSAGE_BYTES {
        return Err(format!(
            "{label} exceeds the hard {MAX_MESSAGE_BYTES}-byte limit"
        ));
    }
    String::from_utf8(bytes).map_err(|_| format!("{label} is not valid UTF-8"))
}

fn targets(
    options: &Options,
    directory: &Path,
    limit: usize,
) -> Result<Vec<CommitTarget>, Failure> {
    let input = |s| ("input", s);
    let git = |e: yaga_core::git::GitError| ("git", e.to_string());
    let literal = if let Some(value) = &options.title {
        commit_source::validate_title(value, "title").map_err(input)?;
        CommitTarget::literal("title".into(), value.clone())
    } else if let Some(value) = &options.common.message {
        if value.len() > MAX_MESSAGE_BYTES {
            return Err(input(format!(
                "explicit message exceeds the hard {MAX_MESSAGE_BYTES}-byte limit"
            )));
        }
        CommitTarget::literal("message".into(), value.clone())
    } else if let Some(file) = options.common.file.as_ref().or(options.edit.as_ref()) {
        let path = resolve_user_path(file).map_err(|e| input(e.to_string()))?;
        let file = File::open(&path)
            .map_err(|e| input(format!("cannot read message file {}: {e}", path.display())))?;
        let mut message = bounded(file, "message file").map_err(input)?;
        if options.edit.is_some() {
            let repo = Repository::open(directory).map_err(git)?;
            let cleaned = repo
                .strip_editor_comments(message.into_bytes(), MAX_MESSAGE_BYTES)
                .map_err(git)?;
            message = String::from_utf8(cleaned)
                .map_err(|_| input("Git editor cleanup returned invalid UTF-8".into()))?;
        }
        CommitTarget::literal(path.to_string_lossy().into_owned(), message)
    } else if options.common.stdin {
        let message = bounded(io::stdin().lock(), "standard input").map_err(input)?;
        CommitTarget::literal("stdin".into(), message)
    } else {
        let repo = Repository::open(directory).map_err(git)?;
        let value = options
            .common
            .range
            .as_deref()
            .or(options.common.commit.as_deref())
            .unwrap_or("HEAD");
        return commit_source::collect(&repo, value, options.common.range.is_some(), limit)
            .map_err(|s| ("git", s));
    };
    Ok(vec![literal])
}

fn check(options: &Options) -> Result<(Vec<Checked>, LoadedConfig), Failure> {
    if [
        options.edit.is_some(),
        options.common.file.is_some(),
        options.title.is_some(),
        options.common.message.is_some(),
        options.common.stdin,
        options.common.commit.is_some(),
        options.common.range.is_some(),
    ]
    .into_iter()
    .filter(|s| *s)
    .count()
        > 1
    {
        return Err((
            "input",
            "choose only one of --title, --edit, --message, --file, --stdin, --commit, or --range"
                .into(),
        ));
    }
    let directory = resolve_user_path(&options.common.repo)
        .map_err(|_| ("input", "commit repository path cannot be resolved".into()))?;
    let loaded = commits::load_config(
        options.common.config.as_deref(),
        &directory,
        commits::global_config_path().as_deref(),
    )
    .map_err(|s| ("configuration", s))?;
    let mut checked = Vec::new();
    for target in targets(options, &directory, loaded.max_commits)? {
        checked.push(
            commit_source::check_target(
                target,
                &loaded,
                &directory,
                options.title.is_some(),
                false,
            )
            .map_err(|e| ("input", e))?,
        );
    }
    Ok((checked, loaded))
}

pub fn run(command: Command) -> ExitCode {
    let options = match command {
        Command::Quality(options) => return quality::run(*options),
        Command::Template(args) => return config::template(args),
        Command::Check(args) => args,
    };
    match check(&options) {
        Ok((results, config)) => {
            if !options.common.quiet {
                println!(
                    "{}",
                    commit_report::render(&results, config.path.as_deref(), options.common.format)
                );
            }
            ExitCode::from(u8::from(results.iter().any(|c| !c.result.valid())))
        }
        Err((kind, message)) => {
            let message = safe_text(&message, 1000);
            match options.common.format {
                Format::Json => eprintln!(
                    "{}",
                    serde_json::to_string_pretty(
                        &json!({"schema_version":1,"error":{"kind":kind,"message":message}})
                    )
                    .unwrap()
                ),
                Format::Text => eprintln!("YAGA {kind} error: {message}"),
                Format::Github => eprintln!(
                    "::error title=YAGA commit policy::YAGA {kind} error: {}",
                    message.replace('%', "%25")
                ),
            }
            ExitCode::from(2)
        }
    }
}
