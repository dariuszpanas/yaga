use clap::{Args, Subcommand, ValueEnum};
use serde_json::json;
use std::{path::PathBuf, process::ExitCode};
use yaga_core::commits::{self, ConfigStarter, TemplateComment};
use yaga_core::text::safe_text;

#[derive(Clone, Copy, ValueEnum)]
pub(crate) enum ConfigFormat {
    Text,
    Json,
}

#[derive(Clone, Copy, ValueEnum)]
pub(crate) enum Starter {
    RecommendedV1,
    TitleV1,
    CompleteMessageV1,
    PlainV1,
}

#[derive(Subcommand)]
pub(crate) enum Command {
    /// Create or preview a standalone policy without overwriting an existing configuration.
    Init {
        #[arg(long, value_enum, default_value = "recommended-v1")]
        starter: Starter,
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        #[arg(long, value_enum, default_value = "text", ignore_case = true)]
        format: ConfigFormat,
        #[arg(long)]
        dry_run: bool,
    },
    /// Show the effective configuration and its source.
    Show {
        #[arg(long = "type")]
        kind: Option<String>,
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long, value_enum, default_value = "text", ignore_case = true)]
        format: ConfigFormat,
    },
}

#[derive(Args)]
pub(crate) struct Template {
    #[arg(long = "type")]
    kind: Option<String>,
    #[arg(long)]
    scope: Option<String>,
    #[arg(long, default_value = ".")]
    repo: PathBuf,
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long,default_value="#",value_parser=["#", ";"])]
    comment_char: String,
}

pub(crate) fn fail(kind: &str, message: &str, format: ConfigFormat) -> ExitCode {
    let message = safe_text(message, 1000);
    match format {
        ConfigFormat::Text => eprintln!("YAGA {kind} error: {message}"),
        ConfigFormat::Json => {
            let doc = json!({"schema_version":1,"error":{"kind":kind,"message":message}});
            eprintln!(
                "{}",
                serde_json::to_string_pretty(&doc).expect("error report is JSON")
            );
        }
    }
    ExitCode::from(2)
}

pub(crate) fn run(command: Command) -> ExitCode {
    let (cfg, dry_run, format, kind) = match command {
        Command::Init {
            repo,
            starter,
            format,
            dry_run,
        } => {
            let starter = match starter {
                Starter::RecommendedV1 => ConfigStarter::RecommendedV1,
                Starter::TitleV1 => ConfigStarter::TitleV1,
                Starter::CompleteMessageV1 => ConfigStarter::CompleteMessageV1,
                Starter::PlainV1 => ConfigStarter::PlainV1,
            };
            (
                commits::initialize_config(&repo, dry_run, starter),
                dry_run,
                format,
                None,
            )
        }
        Command::Show {
            repo,
            config,
            format,
            kind,
        } => (
            commits::load_config(
                config.as_deref(),
                &repo,
                commits::global_config_path().as_deref(),
            ),
            false,
            format,
            kind,
        ),
    };
    let cfg = match cfg {
        Ok(c) => c,
        Err(e) => return fail("configuration", &e, format),
    };
    match commits::render_config(
        &cfg,
        matches!(format, ConfigFormat::Json),
        dry_run,
        kind.as_deref(),
    ) {
        Ok(text) => {
            println!("{text}");
            ExitCode::SUCCESS
        }
        Err(e) => fail("input", &e, format),
    }
}

pub(crate) fn template(args: Template) -> ExitCode {
    let cfg = match commits::load_config(
        args.config.as_deref(),
        &args.repo,
        commits::global_config_path().as_deref(),
    ) {
        Ok(c) => c,
        Err(e) => return fail("configuration", &e, ConfigFormat::Text),
    };
    let marker = if args.comment_char == "#" {
        TemplateComment::Hash
    } else {
        TemplateComment::Semicolon
    };
    match commits::message_template(
        &cfg.policy,
        args.kind.as_deref(),
        args.scope.as_deref(),
        marker,
    ) {
        Ok(text) => {
            print!("{text}");
            ExitCode::SUCCESS
        }
        Err(e) => fail("input", &e, ConfigFormat::Text),
    }
}
