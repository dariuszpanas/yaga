use crate::Format;
use clap::Subcommand;
use serde_json::{Value, json};
use std::{path::PathBuf, process::ExitCode};
use yaga_core::{
    fs::{read_regular_prefix, resolve_user_path},
    gh::PullRequest,
    text::safe_text,
    triage::{self, MAX_BYTES, Snapshot, ascii_json},
};
#[derive(Subcommand)]
pub enum Command {
    /// Evaluate trusted admission policy on complete bounded metadata.
    Check {
        #[arg(long)]
        policy: PathBuf,
        #[arg(long)]
        file: Option<PathBuf>,
        #[arg(long)]
        pr: Option<String>,
        #[arg(long)]
        repo: Option<String>,
        #[arg(long)]
        publish_comment: bool,
        #[arg(long, value_enum, ignore_case = true, default_value = "text")]
        format: Format,
    },
}
fn render(report: &Value, format: Format) -> Result<String, String> {
    if matches!(format, Format::Json) {
        let text = ascii_json(&serde_json::to_string_pretty(report).unwrap());
        if text.len() > MAX_BYTES {
            return Err("triage report exceeds 8 MiB".into());
        }
        return Ok(text);
    }
    let a = &report["admission"];
    let summary = format!(
        "PR triage: {} ({}); {} findings.",
        a["decision"].as_str().unwrap(),
        a["reason"].as_str().unwrap(),
        report["finding_count"]
    );
    if matches!(format, Format::Github) {
        return Ok(format!("::notice title=YAGA PR triage::{summary}"));
    }
    let mut lines = vec![
        summary,
        format!("Head: {}", report["boundary"]["head_sha"].as_str().unwrap()),
    ];
    for f in report["findings"].as_array().unwrap() {
        let detail = f["path"]
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(|| f["pull_request_number"].to_string());
        lines.push(format!(
            "- {}: {}",
            f["code"].as_str().unwrap(),
            safe_text(&detail, 300)
        ));
    }
    Ok(lines.join("\n"))
}
pub fn run(command: Command) -> ExitCode {
    let Command::Check {
        policy,
        file,
        pr,
        repo,
        publish_comment,
        format,
    } = command;
    let result = (|| {
        let input = |s| ("input", s);
        if file.is_some() == pr.is_some() {
            return Err(input(
                "select exactly one triage source: --file or --pr".into(),
            ));
        }
        if repo.is_some() && pr.is_none() {
            return Err(input("triage --repo requires --pr".into()));
        }
        if publish_comment && pr.is_none() {
            return Err(input(
                "triage --publish-comment requires a live --pr".into(),
            ));
        }
        let policy = triage::load_policy(&policy).map_err(|s| ("configuration", s))?;
        let report = if let Some(pr) = pr {
            triage::check_live(
                PullRequest::parse(&pr, repo.as_deref()).map_err(input)?,
                &policy,
                publish_comment,
            )
        } else {
            let path = resolve_user_path(&file.unwrap())
                .map_err(|_| input("cannot resolve explicit triage snapshot".into()))?;
            let raw = read_regular_prefix(&path, MAX_BYTES)
                .map_err(|_| input("cannot read explicit triage snapshot".into()))?;
            Snapshot::decode(&raw).and_then(|snapshot| triage::evaluate(&policy, &snapshot))
        }
        .map_err(input)?;
        let rendered = render(&report, format).map_err(input)?;
        Ok((report["admission"]["decision"] == "allow", rendered))
    })();
    match result {
        Ok((allowed, output)) => {
            println!("{output}");
            ExitCode::from(u8::from(!allowed))
        }
        Err((kind, error)) => {
            let message = safe_text(&error, 500);
            let text=match format{
                Format::Json=>ascii_json(&serde_json::to_string_pretty(&json!({"schema_version":1,"kind":"pull_request_triage","status":"error","valid":false,"error":{"kind":kind,"message":message}})).unwrap()),
                Format::Text=>format!("YAGA triage {kind} error: {message}"),
                Format::Github=>format!("::error title=YAGA PR triage::{}",message.replace('%',"%25")),
            };
            eprintln!("{text}");
            ExitCode::from(2)
        }
    }
}
