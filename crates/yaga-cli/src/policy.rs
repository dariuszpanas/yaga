use super::{Format, config::ConfigFormat};
use clap::Subcommand;
use std::{
    io::{self, IsTerminal},
    path::PathBuf,
    process::ExitCode,
};
use yaga_core::{
    policies::{self, Error, Kind},
    text::safe_text,
};
mod selector;

#[derive(Subcommand)]
pub(crate) enum Command {
    /// Create editable starters for the selected policy families.
    Init {
        policies: Vec<Kind>,
        #[arg(long)]
        all: bool,
        #[arg(long)]
        skip_existing: bool,
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        #[arg(long, value_enum, default_value = "text", ignore_case = true)]
        format: ConfigFormat,
        #[arg(long)]
        dry_run: bool,
    },
    /// Validate explicit policies or discover schema versions in one repository.
    Check {
        paths: Vec<PathBuf>,
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        #[arg(long)]
        strict: bool,
        #[arg(long, value_enum, default_value = "text", ignore_case = true)]
        format: Format,
        #[arg(short, long)]
        quiet: bool,
    },
}

fn error(e: Error, format: Format, init: bool) -> ExitCode {
    let message = safe_text(&e.to_string(), 500);
    let kind = e.kind();
    match format {
        Format::Json => {
            let report = serde_json::json!({"schema_version":1,"kind":if init{"policy_init"}else{"policy_check"},
                "status":"error","valid":false,"error":{"kind":kind,"message":message}});
            eprintln!("{}", policies::pretty_json(&report));
        }
        Format::Github => eprintln!(
            "::error title=YAGA {kind} error::{}",
            message.replace('%', "%25")
        ),
        Format::Text => eprintln!("YAGA {kind} error: {message}"),
    }
    ExitCode::from(2)
}

pub(crate) fn run(command: Command) -> ExitCode {
    match command {
        Command::Init {
            mut policies,
            all,
            repo,
            skip_existing,
            format,
            dry_run,
        } => {
            let format = match format {
                ConfigFormat::Text => Format::Text,
                ConfigFormat::Json => Format::Json,
            };
            if all {
                if !policies.is_empty() {
                    return error(
                        Error::Input("choose either explicit policy names or --all".into()),
                        format,
                        true,
                    );
                }
                policies = Kind::ALL.to_vec();
            } else if policies.is_empty() {
                if !matches!(format, Format::Text)
                    || !io::stdin().is_terminal()
                    || !io::stdout().is_terminal()
                {
                    return error(Error::Input("select one or more policy names, pass --all, or run interactively in a terminal".into()),format,true);
                }
                let existing = match policies::existing_policies(&repo, Kind::ALL) {
                    Ok(e) => e,
                    Err(e) => return error(e, format, true),
                };
                match selector::select(&existing) {
                    Ok(None) => {
                        println!("No policies initialized (cancelled).");
                        return ExitCode::SUCCESS;
                    }
                    Ok(Some(items)) if items.is_empty() => {
                        println!("No policies selected; no files created.");
                        return ExitCode::SUCCESS;
                    }
                    Ok(Some(items)) => {
                        policies = items;
                    }
                    Err(_) => {
                        return error(
                            Error::Input("cannot read interactive policy selection".into()),
                            format,
                            true,
                        );
                    }
                }
            }
            match policies::initialize_policies(&repo, &policies, dry_run, skip_existing) {
                Ok(report) => {
                    println!("{}", report.render(matches!(format, Format::Json)));
                    ExitCode::SUCCESS
                }
                Err(e) => error(e, format, true),
            }
        }
        Command::Check {
            paths,
            strict,
            format,
            repo,
            quiet,
        } => match policies::audit_policies(&repo, &paths, strict) {
            Err(e) => error(e, format, false),
            Ok(report) => {
                let code = report.exit_code();
                let format = match format {
                    Format::Text => policies::Format::Text,
                    Format::Json => policies::Format::Json,
                    Format::Github => policies::Format::Github,
                };
                if code == 2 {
                    eprintln!("{}", report.render(format));
                } else if !quiet {
                    println!("{}", report.render(format));
                }
                ExitCode::from(code)
            }
        },
    }
}
