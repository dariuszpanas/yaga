//! Command adapters for the four explicit committed-tree providers.
mod reports;

use crate::Format;
use clap::{Args, Subcommand};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use yaga_core::fs::resolve_user_path;
use yaga_core::git::{CommittedTreeIdentity, GitError, GitTreeKind, resolve_committed_tree};
use yaga_core::paths::{
    PathError, modes::ModePolicy, portability::PathPolicy, sizes::SizePolicy, tree::TreePolicy,
};
use yaga_core::text::{github_data, json_text, safe_text};

#[derive(Args)]
pub struct TreeArgs {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Check the exact committed tree against an explicit policy.
    Check {
        #[arg(long)]
        policy: PathBuf,
        #[arg(long, allow_hyphen_values = true)]
        revision: String,
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        #[arg(long, value_enum, default_value = "text", ignore_case = true)]
        format: Format,
        #[arg(short, long)]
        quiet: bool,
    },
}

#[derive(Clone, Copy)]
pub enum Provider {
    Tree,
    Path,
    Size,
    Mode,
}

impl Provider {
    fn name(self) -> &'static str {
        match self {
            Self::Tree => "tree",
            Self::Path => "path",
            Self::Mode => "mode",
            Self::Size => "size",
        }
    }
    fn title(self) -> &'static str {
        match self {
            Self::Tree => "Tree",
            Self::Path => "Path",
            Self::Mode => "Mode",
            Self::Size => "Size",
        }
    }
    fn description(self) -> &'static str {
        match self {
            Self::Tree => "committed-tree",
            Self::Path => "committed-path",
            Self::Size => "committed blob-size",
            Self::Mode => "committed-mode",
        }
    }
    fn git(self) -> GitTreeKind {
        match self {
            Self::Tree => GitTreeKind::Tree,
            Self::Path => GitTreeKind::Path,
            Self::Size => GitTreeKind::Size,
            Self::Mode => GitTreeKind::Mode,
        }
    }
}

pub(super) struct Failure(pub(super) &'static str, pub(super) String);

impl From<GitError> for Failure {
    fn from(error: GitError) -> Self {
        Self(error.kind(), error.to_string())
    }
}

pub(super) fn read_policy(input: &Path, provider: &str) -> Result<(PathBuf, Vec<u8>), Failure> {
    let path = resolve_user_path(input).map_err(|_| {
        Failure(
            "configuration",
            format!(
                "cannot resolve {provider} policy {}",
                safe_text(&yaga_core::fs::display_path(input), 300)
            ),
        )
    })?;
    let data = yaga_core::fs::read_regular_prefix(&path, 65536).map_err(|_| {
        Failure(
            "configuration",
            format!(
                "cannot read {provider} policy {}",
                policy_error_path(input, &path)
            ),
        )
    })?;
    if data.len() > 65536 {
        return Err(Failure(
            "configuration",
            format!(
                "{provider} policy exceeds 65536 bytes: {}",
                policy_error_path(input, &path)
            ),
        ));
    }
    Ok((path, data))
}

fn policy_error_path(_input: &Path, resolved: &Path) -> String {
    let display = resolved.to_string_lossy();
    #[cfg(windows)]
    {
        use std::path::{Component, Prefix};
        // The resolver can introduce a verbatim prefix for names which Win32
        // cannot open. Preserve an explicit prefix but hide a resolver-added one.
        let explicit = matches!(_input.components().next(), Some(Component::Prefix(p)) if p.kind().is_verbatim());
        if !explicit && let Some(Component::Prefix(prefix)) = resolved.components().next() {
            match prefix.kind() {
                Prefix::VerbatimDisk(_) => return safe_text(&display[4..], 300),
                Prefix::VerbatimUNC(_, _) => {
                    return safe_text(&format!(r"\\{}", &display[8..]), 300);
                }
                _ => {}
            }
        }
    }
    safe_text(&display, 300)
}

pub(super) fn finish(
    provider: &str,
    format: Format,
    quiet: bool,
    result: Result<(bool, String), Failure>,
) -> ExitCode {
    match result {
        Ok((valid, output)) => {
            if !quiet {
                println!("{output}");
            }
            ExitCode::from(u8::from(!valid))
        }
        Err(Failure(kind, message)) => {
            let message = safe_text(&message, 500);
            let text = format!("YAGA {kind} error: {message}");
            match format {
                Format::Json => eprintln!(
                    "{}",
                    pretty(
                        &json!({"schema_version":1,"kind":format!("{provider}_policy"),"status":"error","valid":false,"error":{"kind":kind,"message":message}})
                    )
                ),
                Format::Github => eprintln!(
                    "::error title=YAGA {provider} policy::{}",
                    github_data(&text, 500)
                ),
                Format::Text => eprintln!("{text}"),
            }
            ExitCode::from(2)
        }
    }
}

pub(super) fn pretty(doc: &Value) -> String {
    serde_json::to_string_pretty(doc).expect("JSON documents serialize")
}

pub(super) use yaga_core::text::github_property as property;

pub(super) fn annotation(code: &str, path: Option<&str>, message: &str) -> String {
    let file = path
        .map(|p| format!("file={},", property(p, 1000)))
        .unwrap_or_default();
    format!(
        "::error {file}title=YAGA {code}::{}",
        github_data(message, 500)
    )
}

enum Policy {
    Tree(TreePolicy),
    Path(PathPolicy),
    Size(SizePolicy),
    Mode(ModePolicy),
}

fn load_policy(provider: Provider, path: &Path) -> Result<(PathBuf, Policy), Failure> {
    let (path, bytes) = read_policy(path, provider.name())?;
    let label = path.to_string_lossy();
    let policy = match provider {
        Provider::Tree => TreePolicy::from_toml_at(&bytes, Some(&label)).map(Policy::Tree),
        Provider::Path => PathPolicy::from_toml_at(&bytes, Some(&label)).map(Policy::Path),
        Provider::Size => SizePolicy::from_toml_at(&bytes, Some(&label)).map(Policy::Size),
        Provider::Mode => ModePolicy::from_toml_at(&bytes, Some(&label)).map(Policy::Mode),
    }
    .map_err(|e| Failure("configuration", e.to_string()))?;
    Ok((path, policy))
}

fn evaluate(
    provider: Provider,
    policy: Policy,
    id: &CommittedTreeIdentity,
    format: Format,
) -> Result<reports::Report, Failure> {
    let output = id.enumerate(provider.git())?;
    match policy {
        Policy::Tree(p) => reports::tree(&p, &output, format),
        Policy::Path(p) => reports::path(&p, &output, format),
        Policy::Mode(p) => reports::mode(&p, &output, id.tree_sha().len(), format),
        Policy::Size(p) => reports::size(&p, &output, id.tree_sha().len(), format),
    }
}

fn document(
    provider: Provider,
    path: &Path,
    id: &CommittedTreeIdentity,
    report: reports::Report,
) -> Value {
    let valid = report.count == 0;
    let identity = json!({
        "policy_path": json_text(&path.to_string_lossy(),1000),
        "repository_path": json_text(&id.repository().to_string_lossy(),1000),
        "revision": json_text(id.revision(),512),
        "commit_sha": id.commit_sha(), "tree_sha": id.tree_sha(),
    });
    let mut doc = json!({
        "schema_version": 1,
        "kind": format!("{}_policy", provider.name()),
        "status": if valid { "passed" } else { "failed" },
        "valid": valid,
    });
    if matches!(provider, Provider::Tree) {
        doc.as_object_mut()
            .unwrap()
            .extend(identity.as_object().unwrap().clone());
    } else {
        doc["identity"] = identity;
    }
    for (key, value) in report.document.as_object().expect("provider report fields") {
        if !matches!(key.as_str(), "missing_required" | "forbidden_paths") {
            doc[key] = value.clone();
        }
    }
    let omitted = report.count - report.items.len();
    doc["diagnostics"] = Value::Array(report.items.into_iter().map(|i| i.document).collect());
    doc["diagnostics_omitted"] = json!(omitted);
    if matches!(provider, Provider::Tree) {
        doc["missing_required"] = report.document["missing_required"].clone();
        doc["forbidden_paths"] = report.document["forbidden_paths"].clone();
    }
    doc
}

pub(crate) struct ChildReport {
    pub document: Value,
    pub text: String,
    pub annotations: Vec<String>,
}

fn render_text(
    provider: Provider,
    path: &Path,
    id: &CommittedTreeIdentity,
    report: &reports::Report,
) -> String {
    let mut lines = vec![
        format!("Policy: {}", safe_text(&path.to_string_lossy(), 1000)),
        format!(
            "Repository: {}",
            safe_text(&id.repository().to_string_lossy(), 1000)
        ),
        format!("Revision: {}", safe_text(id.revision(), 300)),
        format!("Commit: {}", id.commit_sha()),
        format!("Tree: {}", id.tree_sha()),
    ];
    let mut visible: Vec<_> = report.items.iter().take(8).collect();
    if matches!(provider, Provider::Size)
        && report.count > 8
        && let Some(total) = report
            .items
            .last()
            .filter(|f| f.document["code"] == "size.total")
    {
        visible[7] = total;
    }
    let omitted = report.count - visible.len();
    lines.extend(visible.into_iter().map(|i| i.text.clone()));
    if omitted > 0 {
        lines.push(format!("... {omitted} additional diagnostic(s) omitted"));
    }
    lines.push(format!("{} policy: {}", provider.title(), report.summary));
    lines.join("\n")
}

/// Repository aggregates bind Git identity once and retain provider-specific enumeration and reporting.
pub(crate) fn check_resolved(
    provider: Provider,
    path: &Path,
    id: &CommittedTreeIdentity,
) -> Result<ChildReport, Failure> {
    let (path, policy) = load_policy(provider, path)?;
    let report = evaluate(provider, policy, id, Format::Json)?;
    let text = render_text(provider, &path, id, &report);
    let annotations = report
        .items
        .iter()
        .take(50)
        .map(|i| i.github.clone())
        .collect();
    Ok(ChildReport {
        document: document(provider, &path, id, report),
        text,
        annotations,
    })
}

fn check(
    provider: Provider,
    path: &Path,
    repo: &Path,
    revision: &str,
    format: Format,
    quiet: bool,
) -> Result<(bool, String), Failure> {
    let (path, policy) = load_policy(provider, path)?;
    let id = resolve_committed_tree(repo, revision, provider.git())?;
    let report = evaluate(provider, policy, &id, format)?;
    let valid = report.count == 0;
    if quiet {
        return Ok((valid, String::new()));
    }
    let text = match format {
        Format::Json => pretty(&document(provider, &path, &id, report)),
        Format::Text => render_text(provider, &path, &id, &report),
        Format::Github => {
            let omitted = report.count - report.items.len();
            let mut lines: Vec<_> = report.items.into_iter().map(|item| item.github).collect();
            if omitted > 0 {
                lines.push(format!(
                    "::error title=YAGA {} policy::{omitted} additional {} violation(s) omitted",
                    provider.name(),
                    provider.description()
                ));
            }
            lines.push(format!(
                "YAGA {} policy: {}",
                provider.name(),
                report.summary
            ));
            lines.join("\n")
        }
    };
    Ok((valid, text))
}

pub fn run(provider: Provider, args: TreeArgs) -> ExitCode {
    let Command::Check {
        policy,
        revision,
        repo,
        format,
        quiet,
    } = args.command;
    finish(
        provider.name(),
        format,
        quiet,
        check(provider, &policy, &repo, &revision, format, quiet),
    )
}

fn input_error(error: PathError) -> Failure {
    Failure("input", error.to_string())
}
fn git_error(error: PathError) -> Failure {
    let message = match error {
        PathError::InvalidPath => {
            "git returned a non-canonical or unsafe repository-relative path".to_string()
        }
        _ => error.to_string(),
    };
    Failure("git", message)
}
