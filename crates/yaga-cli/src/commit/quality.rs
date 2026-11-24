use super::{Common, Failure, targets};
use crate::Format;
use clap::Args;
use serde_json::json;
use std::process::ExitCode;
use yaga_core::{
    commits::{self, QualityInputMode, QualityProvider, QualityTask},
    fs::resolve_user_path,
    quality,
    text::safe_text,
};

#[derive(Args)]
pub struct Options {
    #[command(flatten)]
    common: Common,
    #[arg(long)]
    provider: Option<String>,
    #[arg(long)]
    task: Option<String>,
    #[arg(long)]
    model: Option<String>,
    #[arg(long)]
    revision: Option<String>,
    #[arg(long)]
    threshold: Option<f64>,
    #[arg(long)]
    offline: bool,
    #[arg(long)]
    region: Option<String>,
    #[arg(long)]
    max_tokens: Option<usize>,
    #[arg(long)]
    max_input_tokens: Option<usize>,
    #[arg(long)]
    input_mode: Option<String>,
}
fn check(options: Options) -> Result<quality::Report, Failure> {
    let common = options.common;
    if [
        common.message.is_some(),
        common.file.is_some(),
        common.stdin,
        common.commit.is_some(),
        common.range.is_some(),
    ]
    .into_iter()
    .filter(|s| *s)
    .count()
        > 1
    {
        return Err((
            "input",
            "choose only one of --message, --file, --stdin, --commit, or --range".into(),
        ));
    }
    let repo = resolve_user_path(&common.repo)
        .map_err(|_| ("input", "commit repository path cannot be resolved".into()))?;
    let mut q = commits::load_config(
        common.config.as_deref(),
        &repo,
        commits::global_config_path().as_deref(),
    )
    .map_err(|s| ("configuration", s))?
    .quality;
    if let Some(provider) = options.provider {
        q.select_provider(match provider.as_str() {
            "bedrock" => QualityProvider::Bedrock,
            "huggingface" => QualityProvider::Huggingface,
            "openai" => QualityProvider::Openai,
            "anthropic" => QualityProvider::Anthropic,
            "gemini" => QualityProvider::Gemini,
            "openai-compatible" => QualityProvider::OpenaiCompatible,
            _ => {
                return Err((
                    "input",
                    "quality provider must be one of: anthropic, bedrock, gemini, huggingface, openai, openai-compatible".into(),
                ));
            }
        });
    }
    if let Some(task) = options.task {
        q.task = match task.as_str() {
            "classification" => QualityTask::Classification,
            "seq2seq" => QualityTask::Seq2seq,
            _ => {
                return Err((
                    "input",
                    "quality task must be one of: classification, seq2seq".into(),
                ));
            }
        }
    }
    if let Some(mode) = options.input_mode {
        q.input_mode = match mode.as_str() {
            "message" => QualityInputMode::Message,
            "title" => QualityInputMode::Title,
            _ => {
                return Err((
                    "input",
                    "quality input mode must be one of: message, title".into(),
                ));
            }
        }
    }
    if let Some(model) = options.model {
        q.model = model
    }
    if let Some(revision) = options.revision {
        q.revision = Some(revision)
    }
    if let Some(threshold) = options.threshold {
        q.threshold = threshold
    }
    if let Some(region) = options.region {
        q.region = Some(region)
    }
    if let Some(max) = options.max_tokens {
        q.max_tokens = max
    }
    if let Some(max) = options.max_input_tokens {
        q.max_input_tokens = max
    }
    quality::validate(&q, options.offline).map_err(|s| ("input", s))?;
    let targets = targets(
        &super::Options {
            edit: None,
            title: None,
            common,
        },
        &repo,
        quality::MAX_RESULTS,
    )?;
    quality::check(targets, q, options.offline).map_err(|s| ("input", s))
}
pub fn run(options: Options) -> ExitCode {
    let format = options.common.format;
    let quiet = options.common.quiet;
    match check(options) {
        Ok(report) => {
            if !quiet {
                println!(
                    "{}",
                    match format {
                        Format::Text => report.text(),
                        Format::Github => report.github(),
                        Format::Json => serde_json::to_string_pretty(&report.document()).unwrap(),
                    }
                );
            }
            ExitCode::from(u8::from(!report.valid()))
        }
        Err((kind, message)) => {
            let message = safe_text(&message, 1000);
            match format {
                Format::Text => eprintln!("YAGA {kind} error: {message}"),
                Format::Json => eprintln!(
                    "{}",
                    serde_json::to_string_pretty(
                        &json!({"schema_version":1,"error":{"kind":kind,"message":message}})
                    )
                    .unwrap()
                ),
                Format::Github => eprintln!(
                    "::error title=YAGA commit quality::{}",
                    message.replace('%', "%25")
                ),
            }
            ExitCode::from(2)
        }
    }
}
