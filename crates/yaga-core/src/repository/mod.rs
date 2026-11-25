//! Explicit repository provider selection and bounded versioned check plans.
mod inspection;
mod plan;
#[cfg(test)]
mod tests;
pub use inspection::{PlanInspection, WorkflowSecurity};
#[cfg(feature = "process")]
pub use plan::load_plan;
pub use plan::{PlanError, RepositoryPlan, parse_plan, parse_plan_table, plan_path_key};

use crate::text::safe_text;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Provider {
    Commit,
    Workflow,
    WorkflowSecurity,
    WorkflowLint,
    Mode,
    Path,
    Size,
    Tree,
}

pub const PROVIDERS: [Provider; 8] = [
    Provider::Commit,
    Provider::Workflow,
    Provider::WorkflowSecurity,
    Provider::WorkflowLint,
    Provider::Mode,
    Provider::Path,
    Provider::Size,
    Provider::Tree,
];
pub const TREE_PROVIDERS: [Provider; 4] = [
    Provider::Mode,
    Provider::Path,
    Provider::Size,
    Provider::Tree,
];
impl Provider {
    pub fn name(self) -> &'static str {
        match self {
            Self::Commit => "commit",
            Self::Workflow => "workflow",
            Self::WorkflowSecurity => "workflow-security",
            Self::WorkflowLint => "workflow-lint",
            Self::Mode => "mode",
            Self::Path => "path",
            Self::Size => "size",
            Self::Tree => "tree",
        }
    }
    pub fn named(name: &str) -> Option<Self> {
        PROVIDERS.iter().copied().find(|p| p.name() == name)
    }
    fn workflow(self) -> bool {
        matches!(
            self,
            Self::Workflow | Self::WorkflowSecurity | Self::WorkflowLint
        )
    }
}

pub const SECURITY_RULES: [&str; 7] = [
    "permissions.explicit",
    "permissions.top_level_write",
    "permissions.write_all",
    "secrets.inherit",
    "checkout.untrusted_ref",
    "checkout.persist_credentials",
    "permissions.pull_request_write",
];

/// Resolve the frozen named profiles and the exact ordered custom rule set.
pub fn normalize_security(
    profile: Option<&str>,
    rules: &[String],
) -> Result<(String, Vec<String>), String> {
    if profile.is_some() && !rules.is_empty() {
        return Err("choose either a workflow security profile or explicit rules".into());
    }
    if rules.is_empty() {
        let profile = profile.unwrap_or("recommended-v1");
        let count = match profile {
            "recommended-v1" => 5,
            "recommended-v2" => 6,
            "recommended-v3" => 7,
            "custom" => {
                return Err("the custom workflow security profile requires explicit rules".into());
            }
            _ => {
                return Err(format!(
                    "unknown workflow security profile: {}",
                    safe_text(profile, 80)
                ));
            }
        };
        return Ok((
            profile.into(),
            SECURITY_RULES[..count]
                .iter()
                .copied()
                .map(str::to_owned)
                .collect(),
        ));
    }
    if rules.len() > SECURITY_RULES.len() {
        return Err("workflow security rule selection exceeds the hard 7-rule limit".into());
    }
    for rule in rules {
        if !SECURITY_RULES.contains(&rule.as_str()) {
            return Err(format!(
                "unknown workflow security rule: {}",
                safe_text(rule, 80)
            ));
        }
    }
    let ordered: Vec<_> = SECURITY_RULES
        .into_iter()
        .filter(|rule| rules.iter().any(|r| r == rule))
        .map(str::to_owned)
        .collect();
    if ordered.len() != rules.len() {
        return Err("workflow security rules must not be repeated".into());
    }
    Ok(("custom".into(), ordered))
}

#[derive(Debug, Default, Clone)]
pub struct Selection {
    pub providers: BTreeSet<Provider>,
    pub workflow_paths: Vec<PathBuf>,
    pub security_profile: Option<String>,
    pub security_rules: Vec<String>,
    pub policy_paths: BTreeMap<Provider, PathBuf>,
}

pub fn normalize_providers(names: &[String]) -> Result<BTreeSet<Provider>, String> {
    if names.is_empty() {
        return Err("select at least one --check provider".into());
    }
    if names.len() > PROVIDERS.len() {
        return Err("repository check provider selection exceeds its hard limit".into());
    }
    let mut providers = BTreeSet::new();
    for name in names {
        let provider = Provider::named(name)
            .ok_or_else(|| format!("unknown repository check provider: {}", safe_text(name, 80)))?;
        if !providers.insert(provider) {
            return Err("repository check providers must not be repeated".into());
        }
    }
    Ok(providers)
}

impl Selection {
    /// Committed-tree providers always require the caller's explicit revision.
    pub fn requires_revision(&self) -> bool {
        self.providers.iter().any(|p| TREE_PROVIDERS.contains(p))
    }

    pub fn validate(
        &self,
        has_config: bool,
        has_commit: bool,
        has_range: bool,
        has_revision: bool,
    ) -> Result<(), String> {
        if self.providers.is_empty() {
            return Err("select at least one --check provider".into());
        }
        if !self.providers.contains(&Provider::Commit) && (has_config || has_commit || has_range) {
            return Err("commit source and --config require the commit provider".into());
        }
        if has_commit && has_range {
            return Err("choose only one of --commit or --range".into());
        }
        if !self.workflow_paths.is_empty() && !self.providers.iter().any(|p| p.workflow()) {
            return Err("--workflow-path requires a workflow check provider".into());
        }
        if (self.security_profile.is_some() || !self.security_rules.is_empty())
            && !self.providers.contains(&Provider::WorkflowSecurity)
        {
            return Err(
                "workflow security profile and rules require the workflow-security provider".into(),
            );
        }
        let has_tree = self.requires_revision();
        if has_tree && !has_revision {
            return Err("--revision is required for committed-tree providers".into());
        }
        if has_revision && !has_tree {
            return Err("--revision requires a committed-tree provider".into());
        }
        for p in TREE_PROVIDERS {
            let name = p.name();
            if self.providers.contains(&p) && !self.policy_paths.contains_key(&p) {
                return Err(format!(
                    "--{name}-policy is required for the {name} provider"
                ));
            }
            if !self.providers.contains(&p) && self.policy_paths.contains_key(&p) {
                return Err(format!("--{name}-policy requires the {name} provider"));
            }
        }
        if self.providers.contains(&Provider::WorkflowSecurity) {
            normalize_security(self.security_profile.as_deref(), &self.security_rules)?;
        }
        Ok(())
    }
}
