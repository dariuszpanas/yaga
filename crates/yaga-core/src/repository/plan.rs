use super::{Provider, SECURITY_RULES, Selection, TREE_PROVIDERS, normalize_security};
#[cfg(feature = "process")]
use crate::fs::{absolute_path, display_path, read_regular_prefix, resolve_user_path};
use crate::text::{casefold, json_text, python_repr, safe_text};
use crate::version::{REQUIREMENT_TYPE_ERROR, check_requirement};
use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

const PLAN_BYTES: usize = 64 * 1024;
const V1_KEYS: [&str; 5] = [
    "plan-version",
    "checks",
    "workflow-paths",
    "workflow-security-profile",
    "workflow-security-rules",
];
const V2_KEYS: [&str; 5] = [
    "required-version",
    "mode-policy",
    "path-policy",
    "size-policy",
    "tree-policy",
];

#[derive(Debug, Clone)]
pub struct RepositoryPlan {
    pub version: u8,
    pub selection: Selection,
}

impl RepositoryPlan {
    /// Validate and return canonical POSIX repository-relative policy paths without accessing
    /// the filesystem.
    pub fn relative_policy_paths(&self) -> Result<BTreeMap<Provider, String>, String> {
        let mut seen = HashSet::new();
        let mut paths = BTreeMap::new();
        for (&provider, path) in &self.selection.policy_paths {
            let key = format!("{}-policy", provider.name());
            let raw = path
                .to_str()
                .ok_or_else(|| format!("{key} contains a non-portable repository-relative path"))?;
            let folded = plan_path_key(raw, &key)?;
            if !seen.insert(folded) {
                return Err("repository policy paths must not be repeated".into());
            }
            paths.insert(provider, raw.to_owned());
        }
        Ok(paths)
    }

    /// Validate and return canonical POSIX repository-relative workflow paths without accessing
    /// the filesystem.
    pub fn relative_workflow_paths(&self) -> Result<Vec<String>, String> {
        let mut seen = HashSet::new();
        let mut paths = Vec::with_capacity(self.selection.workflow_paths.len());
        for path in &self.selection.workflow_paths {
            let raw = path.to_str().ok_or_else(|| {
                "workflow-paths contains a non-portable repository-relative path".to_owned()
            })?;
            let folded = plan_path_key(raw, "workflow-paths")?;
            if !seen.insert(folded) {
                return Err(format!(
                    "workflow-paths contains duplicate path {}",
                    python_repr(raw)
                ));
            }
            paths.push(raw.to_owned());
        }
        Ok(paths)
    }

    /// Bind an on-disk plan's exact policy paths to the runtime repository.
    #[cfg(feature = "process")]
    pub fn bind(mut self, repo: &Path) -> Result<Selection, String> {
        if !self.selection.policy_paths.is_empty() {
            let invalid =
                || "repository plan policy paths must resolve inside the repository".to_owned();
            let repo = resolve_user_path(repo).map_err(|_| invalid())?;
            if !repo.is_dir() {
                return Err(invalid());
            }
            for path in self.selection.policy_paths.values_mut() {
                let resolved = absolute_path(&repo.join(&*path)).map_err(|_| invalid())?;
                if !resolved.starts_with(&repo) {
                    return Err(invalid());
                }
                *path = resolved;
            }
        }
        Ok(self.selection)
    }
}

#[cfg(feature = "process")]
pub fn load_plan(path: &Path) -> Result<RepositoryPlan, String> {
    let path = resolve_user_path(path).map_err(|_| {
        format!(
            "cannot resolve repository check plan {}",
            safe_text(&display_path(path), 300)
        )
    })?;
    let bytes = read_regular_prefix(&path, PLAN_BYTES)
        .map_err(|_| format!("cannot read repository check plan {}", path.display()))?;
    parse_plan(&bytes, &path)
}

pub fn parse_plan(bytes: &[u8], path: &Path) -> Result<RepositoryPlan, String> {
    decode_plan(bytes, path)
}

fn decode_plan(bytes: &[u8], path: &Path) -> Result<RepositoryPlan, String> {
    let path = path.to_string_lossy();
    if bytes.len() > PLAN_BYTES {
        return Err(format!(
            "repository check plan exceeds {PLAN_BYTES} bytes: {path}"
        ));
    }
    let text = std::str::from_utf8(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes))
        .map_err(|_| format!("repository check plan is not valid UTF-8: {path}"))?;
    let doc: toml::Table = toml::from_str(text).map_err(|e| {
        format!(
            "invalid repository check plan TOML in {path}: {}",
            e.message()
        )
    })?;
    let version = doc.get("plan-version").and_then(toml::Value::as_integer);
    if version != Some(1)
        && let Some(req) = doc.get("required-version")
    {
        check_requirement(req.as_str().ok_or(REQUIREMENT_TYPE_ERROR)?)?;
    }
    parse_plan_table(&doc, plan_path_key).map_err(|error| error.message(&path))
}

/// Structured errors keep document values distinct from the filesystem path in diagnostics.
pub enum PlanError<E> {
    At(String),
    UnknownKeys(String),
    NewerVersion(i64),
    UnknownProvider(String),
    DuplicateWorkflowPath(String),
    Security(String),
    Path(E),
}

impl PlanError<String> {
    fn message(self, path: &str) -> String {
        match self {
            Self::At(reason) | Self::Path(reason) => format!("{reason} in {path}"),
            Self::UnknownKeys(keys) => {
                format!("unknown repository check plan key(s) in {path}: {keys}")
            }
            Self::NewerVersion(version) => format!(
                "plan-version must be the integer 1 or 2 in {path} (found newer schema version {version}; update YAGA using its installation manager)"
            ),
            Self::UnknownProvider(name) => format!(
                "unknown repository check provider {} in {path}",
                python_repr(&safe_text(&name, 80))
            ),
            Self::DuplicateWorkflowPath(value) => format!(
                "workflow-paths contains duplicate path {} in {path}",
                python_repr(&value)
            ),
            Self::Security(reason) => format!(
                "invalid workflow security selection in {path}: {}",
                safe_text(&reason, 300)
            ),
        }
    }
}

/// Normalize one host-decoded plan after its version requirement has been checked.
/// The path callback validates portable path syntax and returns its case-insensitive key.
pub fn parse_plan_table<E>(
    doc: &toml::Table,
    mut path_key: impl FnMut(&str, &str) -> Result<String, E>,
) -> Result<RepositoryPlan, PlanError<E>> {
    use PlanError::{
        At, DuplicateWorkflowPath, NewerVersion, Security, UnknownKeys, UnknownProvider,
    };
    let version = doc.get("plan-version").and_then(toml::Value::as_integer);
    let mut unknown = doc
        .keys()
        .map(String::as_str)
        .filter(|k| !(V1_KEYS.contains(k) || (version >= Some(2) && V2_KEYS.contains(k))))
        .collect::<Vec<_>>();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(UnknownKeys(unknown.join(", ")));
    }
    if !doc.contains_key("plan-version") {
        return Err(At("repository check plan is missing plan-version".into()));
    }
    let version = match version {
        Some(v @ (1 | 2)) => v as u8,
        Some(v) if v > 2 => return Err(NewerVersion(v)),
        _ => return Err(At("plan-version must be the integer 1 or 2".into())),
    };
    let mut selection = Selection::default();
    let checks = doc
        .get("checks")
        .ok_or_else(|| At("repository check plan is missing checks".into()))?;
    let checks = array(checks, "checks", if version == 1 { 4 } else { 8 }).map_err(At)?;
    for name in &checks {
        let provider = Provider::named(name)
            .filter(|p| version != 1 || !TREE_PROVIDERS.contains(p))
            .ok_or_else(|| UnknownProvider(name.clone()))?;
        selection.providers.insert(provider);
    }
    if checks.len() != selection.providers.len() {
        return Err(At("repository check providers must not be repeated".into()));
    }
    if let Some(value) = doc.get("workflow-paths") {
        let mut seen = HashSet::new();
        for value in array(value, "workflow-paths", 128).map_err(At)? {
            let key = path_key(&value, "workflow-paths").map_err(PlanError::Path)?;
            if !seen.insert(key) {
                return Err(DuplicateWorkflowPath(value));
            }
            selection.workflow_paths.push(PathBuf::from(value));
        }
    }
    if doc.contains_key("workflow-security-profile") && doc.contains_key("workflow-security-rules")
    {
        return Err(At(
            "choose either workflow-security-profile or workflow-security-rules".into(),
        ));
    }
    if let Some(profile) = doc.get("workflow-security-profile") {
        selection.security_profile = Some(
            profile
                .as_str()
                .ok_or_else(|| At("workflow-security-profile must be a string".into()))?
                .to_owned(),
        );
    }
    if let Some(rules) = doc.get("workflow-security-rules") {
        selection.security_rules =
            array(rules, "workflow-security-rules", SECURITY_RULES.len()).map_err(At)?;
    }
    let (profile, rules) = normalize_security(
        selection.security_profile.as_deref(),
        &selection.security_rules,
    )
    .map_err(Security)?;
    if profile == "custom" {
        selection.security_rules = rules;
    }
    let mut seen = HashSet::new();
    for p in TREE_PROVIDERS {
        let name = p.name();
        let key = format!("{name}-policy");
        let val = doc.get(&key);
        match (selection.providers.contains(&p), val) {
            (true, None) => {
                return Err(At(format!("{key} is required for the {name} check")));
            }
            (false, Some(_)) => return Err(At(format!("{key} requires the {name} check"))),
            (true, Some(value)) => {
                let value = value
                    .as_str()
                    .ok_or_else(|| At(format!("{key} must be a string")))?;
                let folded = path_key(value, &key).map_err(PlanError::Path)?;
                if !seen.insert(folded) {
                    return Err(At("repository policy paths must not be repeated".into()));
                }
                selection.policy_paths.insert(p, PathBuf::from(value));
            }
            _ => {}
        }
    }
    if !selection.workflow_paths.is_empty() && !selection.providers.iter().any(|p| p.workflow()) {
        return Err(At("workflow-paths require a workflow check provider".into()));
    }
    if (selection.security_profile.is_some() || !selection.security_rules.is_empty())
        && !selection.providers.contains(&Provider::WorkflowSecurity)
    {
        return Err(At(
            "workflow security profile and rules require the workflow-security check".into(),
        ));
    }
    Ok(RepositoryPlan { version, selection })
}

fn array(value: &toml::Value, key: &str, max: usize) -> Result<Vec<String>, String> {
    let wrong = || format!("{key} must be an array of strings");
    let items = value.as_array().ok_or_else(wrong)?;
    if !items.iter().all(toml::Value::is_str) {
        return Err(wrong());
    }
    if items.is_empty() {
        return Err(format!("{key} must not be empty"));
    }
    if items.len() > max {
        return Err(format!("{key} exceeds {max} entries"));
    }
    Ok(items
        .iter()
        .filter_map(toml::Value::as_str)
        .map(str::to_owned)
        .collect())
}

pub fn plan_path_key(value: &str, key: &str) -> Result<String, String> {
    let nonportable = || format!("{key} contains a non-portable repository-relative path");
    let invalid = || format!("{key} contains an invalid repository-relative path");
    let workflow = key == "workflow-paths";
    if value.contains('\\')
        || if workflow {
            value.starts_with('~')
        } else {
            value.contains('~')
        }
    {
        return Err(nonportable());
    }
    if value.is_empty()
        || value.starts_with('/')
        || value.len() > 4096
        || value.split('/').count() > 64
    {
        return Err(invalid());
    }
    for part in value.split('/') {
        if part.is_empty() || part == "." {
            return Err(nonportable());
        }
        if part == ".." {
            return Err(invalid());
        }
        let stem = part.split('.').next().unwrap().to_ascii_lowercase();
        let reserved = matches!(stem.as_str(), "aux" | "con" | "nul" | "prn")
            || ["com", "lpt"].into_iter().any(|prefix| {
                stem.strip_prefix(prefix).is_some_and(|n| {
                    matches!(
                        n,
                        "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                    )
                })
            });
        if part.trim() != part
            || part.ends_with('.')
            || part.contains(['<', '>', ':', '"', '|', '?', '*'])
            || reserved
            || json_text(part, part.len()) != part
        {
            return Err(nonportable());
        }
    }
    Ok(casefold(value))
}
