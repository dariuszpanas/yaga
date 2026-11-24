use crate::{Format, commit_report};
use clap::Args;
use serde_json::json;
use std::path::PathBuf;
use std::process::ExitCode;
use yaga_core::{github_commit, text::safe_text};

#[derive(Args)]
pub struct Options {
    /// Read one GitHub pull_request event JSON file.
    #[arg(long)]
    event_file: PathBuf,
    #[arg(long, default_value = ".")]
    repo: PathBuf,
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long, value_enum, default_value = "text", ignore_case = true)]
    format: Format,
}

pub fn run(options: Options) -> ExitCode {
    match github_commit::check(
        &options.event_file,
        &options.repo,
        options.config.as_deref(),
        None,
        None,
    ) {
        Ok(report) => {
            println!(
                "{}",
                commit_report::render_pull_request(&report, options.format)
            );
            ExitCode::from(u8::from(!report.valid()))
        }
        Err((kind, message)) => {
            let message = safe_text(&message, 1000);
            match options.format {
                Format::Text => eprintln!("YAGA {kind} error: {message}"),
                Format::Json => eprintln!(
                    "{}",
                    serde_json::to_string_pretty(
                        &json!({"schema_version":1,"error":{"kind":kind,"message":message}})
                    )
                    .unwrap()
                ),
                Format::Github => eprintln!(
                    "::error title=YAGA commit policy::YAGA {kind} error: {}",
                    message.replace('%', "%25")
                ),
            }
            ExitCode::from(2)
        }
    }
}
