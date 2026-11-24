//! Test qualification selection and source binding. Planning never executes suites.
mod decode;
pub mod diagnostics;
pub mod evaluation;
pub mod handoff;
mod report;
pub use decode::{load_json, parse_plan, parse_plan_with_drive_syntax};
mod selection;
#[cfg(test)]
mod tests;
pub use crate::policies::testing::{Mode, Profile, Rule, Suite, TestPolicy, UnknownPaths};
use crate::{
    fs::{read_regular_prefix, resolve_user_path},
    git::{GitError, read_changed_paths, resolve_repository_tree},
    paths::DriveSyntax,
    text::{python_repr, safe_text},
};
pub use selection::{
    Reason, SelectedSuite, Selection, plan_tests, plan_tests_with_limits, plan_tests_with_path_repr,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{fmt, path::Path};

const MAX_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug)]
pub enum Error {
    Configuration(String),
    Input(String),
    Git(GitError),
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Configuration(s) | Self::Input(s) => f.write_str(s),
            Self::Git(e) => e.fmt(f),
        }
    }
}
impl std::error::Error for Error {}
impl Error {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Configuration(_) => "configuration",
            Self::Input(_) => "input",
            Self::Git(e) => e.kind(),
        }
    }
}
impl From<GitError> for Error {
    fn from(e: GitError) -> Self {
        Self::Git(e)
    }
}
fn is_digest(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn resolve_profile<'a>(p: &'a TestPolicy, name: &str) -> Result<&'a Profile, Error> {
    if !(1..=64).contains(&name.chars().count()) {
        return Err(Error::Configuration(
            "test profile must be an explicit string of 1 through 64 characters".into(),
        ));
    }
    p.profiles
        .iter()
        .find(|(n, _)| n == name)
        .map(|(_, p)| p)
        .ok_or_else(|| {
            Error::Configuration(format!(
                "unknown test profile: {}",
                python_repr(&safe_text(name, 100))
            ))
        })
}
pub fn load_policy(path: &Path) -> Result<TestPolicy, Error> {
    let path = resolve_user_path(path).map_err(|_| {
        Error::Configuration(format!(
            "cannot resolve test policy: {}",
            safe_text(&crate::fs::display_path(path), 300)
        ))
    })?;
    let raw = read_regular_prefix(&path, 128 * 1024).map_err(|_| {
        Error::Configuration(format!(
            "cannot read regular test policy: {}",
            safe_text(&path.to_string_lossy(), 300)
        ))
    })?;
    TestPolicy::parse(&raw).map_err(Error::Configuration)
}

#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub base_commit: Option<String>,
    pub head_commit: Option<String>,
    pub comparison_commit: Option<String>,
    pub execution_commit: String,
    pub execution_tree: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Plan {
    #[serde(rename = "test-plan-version")]
    version: u8,
    pub policy_sha256: String,
    pub bindings_sha256: Option<String>,
    pub source: Source,
    #[serde(flatten)]
    pub selection: Selection,
}

impl Plan {
    fn digest(&self) -> Result<String, Error> {
        let mut value = serde_json::to_value(self).expect("serializable plan");
        value.sort_all_objects();
        semantic_digest(&value)
    }
}

fn semantic_digest(sorted: &Value) -> Result<String, Error> {
    let semantic = bounded(sorted.to_string(), "test report")?;
    Ok(format!("{:x}", Sha256::digest(semantic.as_bytes())))
}

/// Encode the v1 wire representation of an already validated semantic plan.
/// Use `parse_plan` to validate an untrusted envelope or `PlanDocument::new` to
/// bind typed inputs. This serializer neither validates nor attests a selection.
pub fn encode_plan_semantics(mut plan: Value) -> Result<(String, String), Error> {
    plan.sort_all_objects();
    let digest = semantic_digest(&plan)?;
    let encoded = envelope_json(plan, &digest, 1)?;
    Ok((digest, encoded))
}

fn envelope_json(sorted_plan: Value, digest: &str, version: u8) -> Result<String, Error> {
    // Keep the three outer keys in canonical order. Move the potentially large
    // sorted plan into the envelope without cloning or reordering it again.
    let mut fields = serde_json::Map::new();
    fields.insert("plan".into(), sorted_plan);
    fields.insert("plan_digest".into(), Value::String(digest.into()));
    fields.insert("test-plan-version".into(), version.into());
    bounded(Value::Object(fields).to_string(), "test report")
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanDocument {
    #[serde(rename = "test-plan-version")]
    version: u8,
    pub plan_digest: String,
    pub plan: Plan,
    #[serde(skip, default = "native_drive_syntax")]
    drive_syntax: DriveSyntax,
}

fn native_drive_syntax() -> DriveSyntax {
    DriveSyntax::NATIVE
}

fn canonical(mut v: Value) -> String {
    v.sort_all_objects();
    v.to_string()
}
fn bounded(s: String, kind: &str) -> Result<String, Error> {
    if s.len() > MAX_BYTES {
        return Err(Error::Configuration(format!(
            "{kind} exceeds the hard {MAX_BYTES}-byte limit"
        )));
    }
    Ok(s)
}

/// Collect exactly one explicit source and bind the resulting selection to its exact object IDs.
pub fn create_plan(
    repo: &Path,
    policy_path: &Path,
    name: &str,
    range: Option<&str>,
    revision: Option<&str>,
    bindings: Option<&str>,
) -> Result<PlanDocument, Error> {
    if range.is_some() == revision.is_some() {
        return Err(Error::Input(
            "test planning requires exactly one of --range or --revision".into(),
        ));
    }
    if bindings.is_some_and(|s| !is_digest(s)) {
        return Err(Error::Input(
            "bindings-sha256 must be a lowercase 64-character SHA-256 digest".into(),
        ));
    }
    let p = load_policy(policy_path)?;
    let profile = resolve_profile(&p, name)?;
    if revision.is_some() && profile.mode != Mode::Full {
        return Err(Error::Input(
            "--revision requires a full test profile; use --range for selective planning".into(),
        ));
    }
    let changed = range
        .map(|r| read_changed_paths(repo, r, true))
        .transpose()
        .map_err(|e| match e {
            GitError::Git(ref s)
                if s == "changed-path range endpoints must not contain object-path selectors" =>
            {
                Error::Input(
                    "test planning range endpoints must not contain object-path selectors".into(),
                )
            }
            other => other.into(),
        })?;
    let identity = resolve_repository_tree(
        repo,
        changed
            .as_ref()
            .map(|v| v.head_sha.as_str())
            .or(revision)
            .expect("one explicit source"),
    )?;
    let source = Source {
        base_commit: changed.as_ref().map(|v| v.base_sha.clone()),
        head_commit: changed.as_ref().map(|v| v.head_sha.clone()),
        comparison_commit: changed.as_ref().map(|v| v.comparison_sha.clone()),
        execution_commit: identity.commit_sha().into(),
        execution_tree: identity.tree_sha().into(),
    };
    if source
        .head_commit
        .as_ref()
        .is_some_and(|head| head != &source.execution_commit)
    {
        return Err(Error::Git(GitError::Git(
            "plan execution commit must equal the range head".into(),
        )));
    }
    let selection = plan_tests(&p, name, changed.as_ref().map(|v| v.paths.as_slice()))?;
    PlanDocument::new(&p.sha256, bindings, source, selection)
}
impl PlanDocument {
    /// Bind a pure selection to independently supplied policy, registry and source identities.
    pub fn new(
        policy_sha256: &str,
        bindings_sha256: Option<&str>,
        source: Source,
        selection: Selection,
    ) -> Result<Self, Error> {
        let plan = Plan {
            version: 1,
            policy_sha256: policy_sha256.to_owned(),
            bindings_sha256: bindings_sha256.map(str::to_owned),
            source,
            selection,
        };
        plan.validate_semantics(DriveSyntax::NATIVE)?;
        let (plan_digest, _) =
            encode_plan_semantics(serde_json::to_value(&plan).expect("serializable plan"))?;
        Ok(Self {
            version: 1,
            plan_digest,
            plan,
            drive_syntax: DriveSyntax::NATIVE,
        })
    }
    pub fn json(&self) -> Result<String, Error> {
        let mut plan = serde_json::to_value(&self.plan).expect("serializable plan");
        plan.sort_all_objects();
        envelope_json(plan, &self.plan_digest, self.version)
    }
}
pub use report::policy_report;
