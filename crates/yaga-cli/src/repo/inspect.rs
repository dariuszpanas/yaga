use crate::config::{self, ConfigFormat};
use clap::Args;
use std::path::PathBuf;
use std::process::ExitCode;
use yaga_core::repository::{PlanInspection, load_plan};
use yaga_core::text::python_repr;

#[derive(Args)]
pub(super) struct Options {
    /// Explicit versioned repository check plan.
    #[arg(long, value_name = "path")]
    plan: PathBuf,
    /// Report format.
    #[arg(long, value_enum, ignore_case = true, default_value = "text")]
    format: ConfigFormat,
}

fn list(values: &[&str]) -> String {
    if values.is_empty() {
        "none".into()
    } else {
        values.join(", ")
    }
}

fn render_text(plan: &PlanInspection<'_>) {
    println!("Repository plan v{}", plan.plan_version);
    println!("Providers: {}", plan.providers.join(", "));
    for path in plan.workflow_paths {
        println!("Workflow path: {}", python_repr(&path.to_string_lossy()));
    }
    if let Some(security) = &plan.workflow_security {
        println!(
            "Workflow security: {} ({})",
            security.profile,
            security.rules.join(", ")
        );
    }
    for (provider, path) in &plan.policy_paths {
        println!(
            "{provider} policy: {}",
            python_repr(&path.to_string_lossy())
        );
    }
    println!("Required options: {}", list(&plan.required_options));
    println!("Required tools: {}", list(&plan.required_tools));
    if let Some(source) = plan.commit_source_default {
        println!("Default commit source: {source}");
    }
}

pub(super) fn run(options: Options) -> ExitCode {
    let plan = match load_plan(&options.plan) {
        Ok(plan) => plan,
        Err(e) => return config::fail("configuration", &e, options.format),
    };
    let report = match plan.inspect() {
        Ok(report) => report,
        Err(e) => return config::fail("configuration", &e, options.format),
    };
    match options.format {
        ConfigFormat::Json => println!(
            "{}",
            serde_json::to_string_pretty(&report).expect("parsed TOML plan is valid JSON")
        ),
        ConfigFormat::Text => render_text(&report),
    }
    ExitCode::SUCCESS
}
