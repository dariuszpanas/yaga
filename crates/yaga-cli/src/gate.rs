//! Offline, provider-neutral review policy commands.
use clap::{Args, Subcommand};
use serde_json::{Value, json};
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use yaga_core::agent_review::{Aggregate, MAX_BYTES, Outcome, Policy};
use yaga_core::fs::resolve_user_path;
use yaga_core::text::{github_data, safe_text};

#[derive(Args)]
pub struct Gate {
    #[command(subcommand)]
    kind: Kind,
}
#[derive(Subcommand)]
enum Kind {
    AgentReview {
        #[command(subcommand)]
        operation: Agent,
    },
}
#[derive(Subcommand)]
enum Agent {
    /// Invalidate inherited statuses on a trusted lifecycle boundary.
    Invalidate,
    /// Classify the current completed CI and lifecycle pair.
    Prepare,
    /// Record exact protected-environment authorization.
    Authorize,
    /// Request and observe an authorized exact-head review.
    Request,
    /// Observe a previously authorized review.
    Observe,
    /// Revalidate completed CI and review evidence.
    Finalize,
    Policy {
        #[command(subcommand)]
        command: Operation,
    },
}
#[derive(Subcommand)]
enum Operation {
    /// Validate named review lenses without contacting a provider.
    Check(ReportArgs),
    /// Expand named review lenses into deterministic adapter work items.
    Plan(ReportArgs),
    /// Render a complete pending receipt scaffold.
    Template {
        #[arg(long)]
        file: PathBuf,
    },
    /// Validate a complete set of adapter results against the exact policy.
    Evaluate {
        #[command(flatten)]
        options: ReportArgs,
        #[arg(long)]
        results: PathBuf,
    },
}
#[derive(Args)]
struct ReportArgs {
    #[arg(long)]
    file: PathBuf,
    #[arg(long, default_value = "text")]
    format: String,
}
type Failure = (&'static str, String);
fn read(path: &Path, kind: &str) -> Result<(PathBuf, Vec<u8>), Failure> {
    let path = resolve_user_path(path).map_err(|e| {
        (
            "configuration",
            format!("cannot resolve Agent review {kind}: {e}"),
        )
    })?;
    let mut bytes = Vec::new();
    File::open(&path)
        .and_then(|f| f.take((MAX_BYTES + 1) as u64).read_to_end(&mut bytes))
        .map_err(|e| {
            (
                "configuration",
                format!("cannot read Agent review {kind} {}: {e}", path.display()),
            )
        })?;
    Ok((path, bytes))
}
fn pretty(v: &Value) -> String {
    serde_json::to_string_pretty(v).unwrap()
}
pub(crate) fn render_result(a: &Aggregate, format: &str) -> Result<String, Failure> {
    if format == "json" {
        return Ok(serde_json::to_string_pretty(a).unwrap());
    }
    let mut lines = Vec::new();
    let groups = [
        ("Blocking failures", "error", &a.blocking_failures),
        ("Blocking pending", "error", &a.blocking_pending),
        ("Advisory failures", "warning", &a.advisory_failures),
        ("Advisory pending", "warning", &a.advisory_pending),
    ];
    match format {
        "text" => {
            lines.push(format!("Agent review result: {}.", a.state.as_str()));
            lines.push(format!("Plan digest: {}", a.plan_digest));
            for r in &a.results {
                let detail = r
                    .summary
                    .as_deref()
                    .map(|s| format!(": {}", safe_text(s, 1000)))
                    .unwrap_or_default();
                lines.push(format!("- {}: {}{detail}", r.lens, r.outcome.as_str()));
            }
            for (label, _, items) in groups {
                if !items.is_empty() {
                    lines.push(format!("{label}: {}", items.join(", ")));
                }
            }
        }
        "github" => {
            for (_, level, names) in groups {
                for name in names {
                    let item = a.results.iter().find(|r| r.lens == *name).unwrap();
                    let message = format!(
                        "{}: {}",
                        item.outcome.as_str(),
                        item.summary
                            .as_deref()
                            .unwrap_or("provider returned no summary")
                    );
                    lines.push(format!(
                        "::{level} title=YAGA Agent review%3A {name}::{}",
                        github_data(&message, 500)
                    ));
                }
            }
            lines.push(format!("::notice title=YAGA Agent review::Agent review result: {}; {} blocking failure(s), {} blocking pending, {} advisory failure(s), {} advisory pending; plan {}.",
                a.state.as_str(),a.blocking_failures.len(),a.blocking_pending.len(),a.advisory_failures.len(),a.advisory_pending.len(),a.plan_digest));
        }
        _ => {
            return Err((
                "input",
                "output format must be text, json, or github".into(),
            ));
        }
    }
    Ok(lines.join("\n"))
}
fn render(operation: &Operation, policy: &Policy, format: &str) -> Result<(bool, String), Failure> {
    let output = match operation {
        Operation::Evaluate { results, .. } => {
            let (path, bytes) = read(results, "results")?;
            let a = policy
                .evaluate(&bytes, &path)
                .map_err(|s| ("configuration", s))?;
            return Ok((a.state == Outcome::Passed, render_result(&a, format)?));
        }
        Operation::Template { .. } => pretty(&policy.template()),
        Operation::Plan(_) if format == "json" => pretty(&policy.plan()),
        Operation::Check(_) if format == "json" => pretty(&policy.check()),
        _ if format != "text" => {
            return Err(("input", "output format must be text or json".into()));
        }
        Operation::Check(_) => {
            let check = policy.check();
            let required = check["required"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap())
                .collect::<Vec<_>>();
            format!(
                "Agent review policy passed: {} lens(es), {} required ({}), aggregation {}.",
                check["lens_count"],
                required.len(),
                required.join(", "),
                check["aggregation"].as_str().unwrap()
            )
        }
        Operation::Plan(_) => {
            let plan = policy.plan();
            let lenses = plan["lenses"].as_array().unwrap();
            let mut lines = vec![
                format!(
                    "Agent review plan: {} lens(es), aggregation {}.",
                    lenses.len(),
                    plan["aggregation"].as_str().unwrap()
                ),
                format!("Plan digest: {}", plan["plan_digest"].as_str().unwrap()),
            ];
            for l in lenses {
                lines.push(format!(
                    "- {} [{}, {}] via {}",
                    l["name"].as_str().unwrap(),
                    if l["required"] == true {
                        "required"
                    } else {
                        "advisory"
                    },
                    l["outcome"].as_str().unwrap(),
                    l["preset"].as_str().unwrap()
                ));
            }
            lines.join("\n")
        }
    };
    Ok((true, output))
}
pub fn run(args: Gate) -> ExitCode {
    let Kind::AgentReview { operation } = args.kind;
    let command = match operation {
        Agent::Policy { command } => command,
        other => {
            let name = match other {
                Agent::Invalidate => "invalidate",
                Agent::Prepare => "prepare",
                Agent::Authorize => "authorize",
                Agent::Request => "request",
                Agent::Observe => "observe",
                Agent::Finalize => "finalize",
                Agent::Policy { .. } => unreachable!(),
            };
            return match yaga_core::agent_review::github::runtime::run_action(name) {
                Ok((n, message)) => {
                    println!("{message}");
                    ExitCode::from(n)
                }
                Err(e) => {
                    eprintln!("YAGA failed: {}", safe_text(&e, 1000));
                    ExitCode::from(1)
                }
            };
        }
    };
    let (file, format) = match &command {
        Operation::Template { file } => (file, "text"),
        Operation::Plan(o) | Operation::Check(o) | Operation::Evaluate { options: o, .. } => {
            (&o.file, o.format.as_str())
        }
    };
    let format = format.to_ascii_lowercase();
    let result = read(file, "policy")
        .and_then(|(p, bytes)| Policy::parse(&bytes, &p).map_err(|s| ("configuration", s)))
        .and_then(|p| render(&command, &p, &format));
    match result {
        Ok((passed, output)) => {
            println!("{output}");
            ExitCode::from(u8::from(!passed))
        }
        Err((kind, error)) => {
            let message = safe_text(&error, 1000);
            match format.as_str() {
                "json" => eprintln!(
                    "{}",
                    pretty(&json!({"schema_version":1,"error":{"kind":kind,"message":message}}))
                ),
                "github" => eprintln!(
                    "::error title=YAGA Agent review::{}",
                    message.replace('%', "%25")
                ),
                _ => eprintln!("YAGA {kind} error: {message}"),
            }
            ExitCode::from(2)
        }
    }
}
