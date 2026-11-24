use super::{Provider, RepositoryPlan, normalize_security};
use serde::Serialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Execution requirements of a parsed repository plan. Paths remain relative to the runtime repo.
#[derive(Debug, Serialize)]
pub struct PlanInspection<'a> {
    pub schema_version: u8,
    pub kind: &'static str,
    pub plan_version: u8,
    pub providers: Vec<&'static str>,
    pub workflow_paths: &'a [PathBuf],
    pub workflow_security: Option<WorkflowSecurity>,
    pub policy_paths: BTreeMap<&'static str, &'a Path>,
    pub required_options: Vec<&'static str>,
    pub required_tools: Vec<&'static str>,
    pub commit_source_default: Option<&'static str>,
}

#[derive(Debug, Serialize)]
pub struct WorkflowSecurity {
    pub profile: String,
    pub rules: Vec<String>,
}

impl RepositoryPlan {
    /// Describe what execution would require without resolving files or inspecting a repository.
    pub fn inspect(&self) -> Result<PlanInspection<'_>, String> {
        let selected = &self.selection;
        let providers = &selected.providers;
        let workflow_security = if providers.contains(&Provider::WorkflowSecurity) {
            let (profile, rules) = normalize_security(
                selected.security_profile.as_deref(),
                &selected.security_rules,
            )?;
            Some(WorkflowSecurity { profile, rules })
        } else {
            None
        };
        let mut required_options = Vec::new();
        let mut required_tools = Vec::new();
        let revision = selected.requires_revision();
        if revision {
            required_options.push("--revision");
        }
        let commit = providers.contains(&Provider::Commit);
        if commit || revision {
            required_tools.push("git");
        }
        if providers.contains(&Provider::WorkflowLint) {
            required_tools.push("docker");
        }
        Ok(PlanInspection {
            schema_version: 1,
            kind: "repository_plan",
            plan_version: self.version,
            providers: providers.iter().map(|p| p.name()).collect(),
            workflow_paths: &selected.workflow_paths,
            workflow_security,
            policy_paths: selected
                .policy_paths
                .iter()
                .map(|(p, path)| (p.name(), path.as_path()))
                .collect(),
            required_options,
            required_tools,
            commit_source_default: commit.then_some("HEAD"),
        })
    }
}
