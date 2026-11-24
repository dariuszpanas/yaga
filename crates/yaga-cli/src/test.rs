use clap::{Args, Subcommand, ValueEnum};
use serde_json::json;
use std::{path::PathBuf, process::ExitCode};
use yaga_core::{
    test_planning::{self, Error},
    text::safe_text,
};

#[derive(Clone, Copy, ValueEnum)]
enum Format {
    Text,
    Json,
}
#[derive(Args)]
pub struct Output {
    #[arg(long, value_enum, ignore_case = true, default_value = "text")]
    format: Format,
    #[arg(short, long)]
    quiet: bool,
}
#[derive(Subcommand)]
pub enum Policy {
    /// Validate an explicit policy without Git access.
    Check {
        #[arg(long)]
        file: PathBuf,
        #[command(flatten)]
        output: Output,
    },
}
#[derive(Subcommand)]
pub enum Command {
    /// Evaluate explicit result receipts against independently supplied identities.
    Evaluate(Box<Evaluate>),
    /// Validate one explicit test qualification policy.
    Policy {
        #[command(subcommand)]
        command: Policy,
    },
    /// Select suites for one explicit source without executing tests.
    Plan {
        #[arg(long)]
        policy: PathBuf,
        #[arg(long)]
        profile: String,
        #[arg(long, allow_hyphen_values = true)]
        range: Option<String>,
        #[arg(long, allow_hyphen_values = true)]
        revision: Option<String>,
        #[arg(long, default_value = ".")]
        repo: PathBuf,
        #[arg(long)]
        bindings_sha256: Option<String>,
        #[command(flatten)]
        output: Output,
    },
}
#[derive(Args)]
pub struct Evaluate {
    #[arg(long)]
    plan: PathBuf,
    #[arg(long)]
    results: PathBuf,
    #[arg(long)]
    policy_sha256: String,
    #[arg(long)]
    bindings_sha256: String,
    #[arg(long)]
    execution_commit: String,
    #[arg(long)]
    execution_tree: String,
    #[arg(long)]
    run_id: String,
    #[arg(long, allow_hyphen_values = true)]
    attempt: String,
    #[command(flatten)]
    output: Output,
}
impl Evaluate {
    fn evaluate(&self) -> Result<test_planning::evaluation::Evaluation, Error> {
        use test_planning::evaluation::{Expected, evaluate, parse_attempt, parse_results};
        let attempt = parse_attempt(&self.attempt)?;
        let plan = test_planning::parse_plan(test_planning::load_json(&self.plan)?)?;
        let results = parse_results(test_planning::load_json(&self.results)?)?;
        evaluate(
            &plan,
            &results,
            Expected {
                policy_sha256: &self.policy_sha256,
                bindings_sha256: &self.bindings_sha256,
                execution_commit: &self.execution_commit,
                execution_tree: &self.execution_tree,
                run_id: &self.run_id,
                attempt,
            },
        )
    }
}
impl Output {
    fn json(&self) -> bool {
        matches!(self.format, Format::Json)
    }
    fn finish(&self, kind: &str, result: Result<String, Error>) -> ExitCode {
        match result {
            Ok(s) => {
                if !self.quiet {
                    if self.json() {
                        print!("{s}");
                    } else {
                        println!("{s}");
                    }
                }
                ExitCode::SUCCESS
            }
            Err(e) => {
                let message = safe_text(&e.to_string(), 1000);
                if self.json() {
                    let mut v = json!({"schema_version":1,"kind":kind,"status":"error","valid":false,"error":{"kind":e.kind(),"message":message}});
                    v.sort_all_objects();
                    eprintln!("{v}");
                } else {
                    eprintln!("YAGA {} error: {message}", e.kind());
                }
                ExitCode::from(2)
            }
        }
    }
}
pub fn run(command: Command) -> ExitCode {
    match command {
        Command::Evaluate(args) => match args.evaluate() {
            Err(e) => args.output.finish("test_evaluation", Err(e)),
            Ok(report) => {
                if !args.output.quiet || !report.passed {
                    if args.output.json() {
                        println!("{}", report.json.trim_end_matches('\n'));
                    } else {
                        println!("{}", report.text);
                    }
                }
                ExitCode::from(u8::from(!report.passed))
            }
        },
        Command::Policy {
            command: Policy::Check { file, output },
        } => output.finish(
            "test_policy",
            test_planning::load_policy(&file)
                .map(|p| test_planning::policy_report(&p, output.json())),
        ),
        Command::Plan {
            policy,
            profile,
            range,
            revision,
            repo,
            bindings_sha256,
            output,
        } => {
            let plan = test_planning::create_plan(
                &repo,
                &policy,
                &profile,
                range.as_deref(),
                revision.as_deref(),
                bindings_sha256.as_deref(),
            );
            output.finish(
                "test_plan",
                plan.and_then(|p| {
                    if output.json() {
                        p.json()
                    } else {
                        Ok(p.text())
                    }
                }),
            )
        }
    }
}
