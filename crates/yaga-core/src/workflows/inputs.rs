use crate::{
    fs::{absolute_path, resolve_user_path, user_path::expand_user_path},
    text::safe_text,
};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

pub const MAX_WORKFLOW_BYTES: usize = 1024 * 1024;

#[derive(Debug)]
pub struct WorkflowInput {
    pub path: PathBuf,
    pub relative_path: String,
    pub content: Vec<u8>,
}

pub(super) fn workflow_suffix(path: &Path) -> bool {
    path.extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("yaml") || ext.eq_ignore_ascii_case("yml"))
}

/// Bounds may be tightened for embedding applications, up to the public hard limits.
#[derive(Copy, Clone)]
pub struct DiscoveryLimits {
    files: usize,
    entries: usize,
    bytes: usize,
    components: usize,
}
impl DiscoveryLimits {
    pub fn bounded(files: usize, entries: usize, bytes: usize, components: usize) -> Self {
        Self {
            files: files.min(128),
            entries: entries.min(1024),
            bytes: bytes.min(4096),
            components: components.min(64),
        }
    }
}
impl Default for DiscoveryLimits {
    fn default() -> Self {
        Self::bounded(128, 1024, 4096, 64)
    }
}

fn add_file(
    found: &mut BTreeMap<String, PathBuf>,
    repo: &Path,
    path: PathBuf,
    limit: DiscoveryLimits,
) -> Result<(), String> {
    if !workflow_suffix(&path) {
        return Err("workflow files must use a .yml or .yaml extension".into());
    }
    let relative = path
        .strip_prefix(repo)
        .map_err(|_| "workflow inputs must resolve inside the repository")?;
    let parts = relative
        .components()
        .map(|p| {
            p.as_os_str()
                .to_str()
                .ok_or("workflow path is not valid UTF-8")
        })
        .collect::<Result<Vec<_>, _>>()?;
    let label = parts.join("/");
    if parts.is_empty() || parts.len() > limit.components || label.len() > limit.bytes {
        return Err("workflow path is not a bounded repository-relative path".into());
    }
    found.insert(label, path);
    if found.len() > limit.files {
        return Err(format!(
            "workflow selection exceeds the hard {}-file limit",
            limit.files
        ));
    }
    Ok(())
}

pub fn discover_inputs(
    repository: &Path,
    selections: &[PathBuf],
    limit: DiscoveryLimits,
) -> Result<BTreeMap<String, PathBuf>, String> {
    discover_inputs_with(repository, selections, limit, None)
}

/// An embedding interpreter can resolve home aliases in selection order and supply
/// its platform's display spelling. Physical containment is still checked here.
pub type HostSelection<'a> = &'a mut dyn FnMut(usize) -> Option<(PathBuf, String)>;

pub fn discover_inputs_with(
    repository: &Path,
    selections: &[PathBuf],
    limit: DiscoveryLimits,
    mut host_selection: Option<HostSelection<'_>>,
) -> Result<BTreeMap<String, PathBuf>, String> {
    let repo =
        resolve_user_path(repository).map_err(|_| "workflow repository path cannot be resolved")?;
    if !repo.is_dir() {
        return Err("workflow repository directory does not exist".into());
    }
    if selections.len() > limit.files {
        return Err(format!(
            "workflow selection exceeds the hard {}-path limit",
            limit.files
        ));
    }
    let default = [Path::new(".github").join("workflows")];
    let selections = if selections.is_empty() {
        host_selection = None;
        &default
    } else {
        selections
    };
    let mut found = BTreeMap::new();
    for (index, selected) in selections.iter().enumerate() {
        let boundary = "workflow inputs must resolve inside the repository";
        let (candidate, label) = if let Some(select) = host_selection.as_mut() {
            let (path, label) = select(index).ok_or(boundary)?;
            (repo.join(path), Some(label))
        } else {
            (
                repo.join(expand_user_path(selected).map_err(|_| boundary)?),
                None,
            )
        };
        let path = soft_canonicalize::soft_canonicalize(candidate).map_err(|_| boundary)?;
        if !path.starts_with(&repo) {
            return Err("workflow inputs must resolve inside the repository".into());
        }
        if path.is_file() {
            add_file(&mut found, &repo, path, limit)?;
            continue;
        }
        let label = safe_text(
            &label.unwrap_or_else(|| crate::fs::display_path(selected)),
            200,
        );
        if !path.is_dir() {
            return Err(format!("workflow input does not exist: {label}"));
        }
        let directory = fs::read_dir(&path).map_err(|_| "workflow directory cannot be read")?;
        let mut matched = false;
        for (i, entry) in directory.enumerate() {
            if i >= limit.entries {
                return Err(format!(
                    "workflow directory exceeds the hard {}-entry limit",
                    limit.entries
                ));
            }
            let path = entry
                .map_err(|_| "workflow directory cannot be read")?
                .path();
            if workflow_suffix(&path) && path.is_file() {
                add_file(
                    &mut found,
                    &repo,
                    absolute_path(&path).map_err(|_| "workflow directory cannot be read")?,
                    limit,
                )?;
                matched = true;
            }
        }
        if !matched {
            return Err(format!(
                "workflow directory contains no .yml or .yaml files: {label}"
            ));
        }
    }
    Ok(found)
}
pub fn load_inputs(
    repository: &Path,
    selections: &[PathBuf],
) -> Result<Vec<WorkflowInput>, String> {
    let found = discover_inputs(repository, selections, DiscoveryLimits::default())?;
    let mut total = 0;
    let mut inputs = Vec::new();
    for (relative_path, path) in found {
        let content = crate::fs::read_regular_prefix(&path, MAX_WORKFLOW_BYTES)
            .map_err(|_| format!("cannot read workflow {}", safe_text(&relative_path, 200)))?;
        total += content.len();
        if total > 8 * MAX_WORKFLOW_BYTES {
            return Err("workflow inputs exceed the hard 8388608-byte total limit".into());
        }
        if content.len() > MAX_WORKFLOW_BYTES {
            return Err(format!(
                "{} exceeds the hard {MAX_WORKFLOW_BYTES}-byte limit",
                safe_text(&relative_path, 160)
            ));
        }
        inputs.push(WorkflowInput {
            path,
            relative_path,
            content,
        });
    }
    Ok(inputs)
}
