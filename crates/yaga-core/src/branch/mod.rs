//! Check an explicitly supplied short branch name against ordered, portable patterns.

mod patterns;
mod policy;
mod reporting;
#[cfg(test)]
mod tests;

use std::collections::HashSet;
use std::fmt;
use std::path::{Path, PathBuf};

use crate::paths::{PathComponents, Pattern, WorkBudget};

pub use patterns::{
    is_portable as is_portable_branch_name, match_branch_pattern, validate_branch_pattern,
};
pub use policy::{load_branch_policy, parse_branch_policy, parse_branch_table};
use reporting::DisplayField;
pub use reporting::OutputFormat;

pub const MAX_BRANCH_NAME_BYTES: usize = 244;
pub const MAX_BRANCH_COMPONENTS: usize = 32;
pub const MAX_BRANCH_PATTERNS: usize = 64;
pub const MAX_BRANCH_POLICY_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone)]
pub struct BranchPolicy {
    patterns: Vec<Pattern>,
}

impl BranchPolicy {
    /// Construct a schema-v1 policy from one through 64 unique, ordered patterns.
    pub fn new(allowed_patterns: Vec<String>) -> Result<Self, String> {
        if !(1..=MAX_BRANCH_PATTERNS).contains(&allowed_patterns.len()) {
            return Err(format!(
                "branch policy requires 1 through {MAX_BRANCH_PATTERNS} allowed patterns"
            ));
        }
        if allowed_patterns.iter().collect::<HashSet<_>>().len() != allowed_patterns.len() {
            return Err("branch policy allowed patterns must be unique".into());
        }
        let mut patterns = Vec::with_capacity(allowed_patterns.len());
        for pattern in &allowed_patterns {
            validate_branch_pattern(pattern)?;
            patterns.push(Pattern::compile(pattern).map_err(|error| error.to_string())?);
        }
        Ok(Self { patterns })
    }

    pub fn allowed_patterns(&self) -> impl ExactSizeIterator<Item = &str> {
        self.patterns.iter().map(Pattern::as_str)
    }
}

#[derive(Debug, Clone)]
pub struct LoadedBranchPolicy {
    pub policy: BranchPolicy,
    pub path: PathBuf,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BranchError {
    Input(String),
    Configuration(String),
}

impl BranchError {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Input(_) => "input",
            Self::Configuration(_) => "configuration",
        }
    }
}

impl fmt::Display for BranchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Input(message) | Self::Configuration(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for BranchError {}

#[derive(Debug, Copy, Clone, PartialEq, Eq, serde::Serialize)]
pub struct BranchDiagnostic {
    pub code: &'static str,
    pub message: &'static str,
}

const SYNTAX: BranchDiagnostic = BranchDiagnostic {
    code: "branch.syntax",
    message: "branch name does not use the portable YAGA branch syntax",
};
const ALLOWED: BranchDiagnostic = BranchDiagnostic {
    code: "branch.allowed",
    message: "branch name is not allowed by any configured pattern",
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BranchReport {
    branch: String,
    outcome: Result<String, BranchDiagnostic>,
}

impl BranchReport {
    pub fn valid(&self) -> bool {
        self.outcome.is_ok()
    }

    pub fn branch(&self) -> &str {
        &self.branch
    }

    pub fn matched_pattern(&self) -> Option<&str> {
        self.outcome.as_deref().ok()
    }

    pub fn diagnostic(&self) -> Option<BranchDiagnostic> {
        self.outcome.as_ref().err().copied()
    }
}

/// Check transport bounds, then syntax, then admission. No filesystem or environment access.
pub fn check_branch_name(policy: &BranchPolicy, branch: &str) -> Result<BranchReport, BranchError> {
    patterns::validate_transport(branch).map_err(BranchError::Input)?;
    let outcome = if patterns::is_portable(branch) {
        let components =
            PathComponents::parse(branch).map_err(|error| BranchError::Input(error.to_string()))?;
        let mut result = Err(ALLOWED);
        for pattern in &policy.patterns {
            let matched = pattern
                .matches(&components, &mut WorkBudget::default())
                .map_err(|error| BranchError::Input(error.to_string()))?;
            if matched {
                result = Ok(pattern.as_str().to_owned());
                break;
            }
        }
        result
    } else {
        Err(SYNTAX)
    };
    Ok(BranchReport {
        branch: branch.to_owned(),
        outcome,
    })
}

#[derive(Debug)]
pub struct CheckedBranch {
    pub report: BranchReport,
    policy: LoadedBranchPolicy,
}

pub fn check_branch(policy_path: &Path, branch: &str) -> Result<CheckedBranch, BranchError> {
    let policy = load_branch_policy(policy_path)?;
    let report = check_branch_name(&policy.policy, branch)?;
    Ok(CheckedBranch { policy, report })
}
