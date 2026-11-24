//! Named inputs and structured failures for an embedding runtime that already owns
//! workflow discovery, Docker environment discovery, signals and report decoding.

use super::{runtime::Docker, snapshot::Snapshot};
use crate::fs::absolute_path;
use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
#[cfg(test)]
mod tests;

/// A snapshot of already-loaded workflow and support files. The order of
/// `selected` is the order in which the caller receives diagnostic responses.
pub struct PreparedLint {
    pub selected: Vec<String>,
    pub files: BTreeMap<String, Vec<u8>>,
    pub directories: BTreeSet<String>,
}

/// A Docker executable and child environment captured by the trusted host.
/// The executable, helper search path and optional Docker config are checked
/// again against the physical repository before any process starts.
pub struct VettedDocker {
    pub executable: PathBuf,
    pub environment: Vec<(OsString, OsString)>,
}

pub(super) fn trusted_executable(repo: &Path, path: &Path) -> Option<PathBuf> {
    if !path.is_absolute() {
        return None;
    }
    let path = absolute_path(path).ok()?;
    let metadata = path.metadata().ok()?;
    if !metadata.is_file() || path.starts_with(repo) {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            return None;
        }
    }
    Some(path)
}

pub(super) fn trusted_config(repo: &Path, path: &Path) -> Result<PathBuf, String> {
    let error = "DOCKER_CONFIG must resolve outside the repository";
    if !path.is_absolute() {
        return Err(error.into());
    }
    let path = absolute_path(path).map_err(|_| error)?;
    if path.starts_with(repo) {
        return Err(error.into());
    }
    Ok(path)
}

impl VettedDocker {
    pub(super) fn validate(&self, repo: &Path) -> Result<(PathBuf, Option<PathBuf>), String> {
        let unavailable = "Docker is required for workflow linting";
        let executable = trusted_executable(repo, &self.executable).ok_or(unavailable)?;
        let mut config = None;
        let mut helper_path = None;
        let mut keys = std::collections::HashSet::new();
        for (key, value) in &self.environment {
            let key_bytes = key.as_encoded_bytes();
            if key_bytes.is_empty()
                || key_bytes.contains(&b'=')
                || key_bytes.contains(&0)
                || value.as_encoded_bytes().contains(&0)
            {
                return Err("Docker environment is invalid".into());
            }
            #[cfg(windows)]
            let name = key.to_ascii_uppercase();
            #[cfg(not(windows))]
            let name = key.clone();
            if !keys.insert(name.clone()) {
                return Err("Docker environment contains a duplicate key".into());
            }
            if name == OsStr::new("PATH") {
                helper_path = Some(value);
            }
            if name == OsStr::new("DOCKER_CONFIG") {
                config = Some(trusted_config(repo, Path::new(value))?);
            }
        }
        let parent = executable.parent().ok_or(unavailable)?;
        let valid_path = helper_path.is_some_and(|value| {
            let mut paths = std::env::split_paths(value);
            paths
                .next()
                .is_some_and(|p| p.is_absolute() && absolute_path(&p).is_ok_and(|p| p == parent))
                && paths.next().is_none()
        });
        if !valid_path || !std::env::split_paths(parent).eq(std::iter::once(parent.to_owned())) {
            return Err(
                "Docker helper search path must contain only the trusted executable directory"
                    .into(),
            );
        }
        Ok((executable, config))
    }
}

/// Failures preserve the original operation independently of each enclosing
/// cleanup failure. Control bytes and OS details are for host exception causes.
#[derive(Clone, PartialEq, Eq)]
pub enum LifecycleError {
    Operation(String),
    Interrupted,
    NonAsciiControl(Vec<u8>),
    ClientIo {
        message: String,
        os_code: i32,
        executable: Option<PathBuf>,
    },
    Cleanup {
        previous: Option<Box<LifecycleError>>,
        message: String,
        cause: Box<LifecycleError>,
    },
}

impl fmt::Debug for LifecycleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Never log the original bytes from an untrusted Docker response.
        fmt::Display::fmt(self, f)
    }
}

impl fmt::Display for LifecycleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Operation(message) | Self::ClientIo { message, .. } => f.write_str(message),
            Self::NonAsciiControl(_) => f.write_str("Docker returned non-ASCII control output"),
            Self::Interrupted => f.write_str("actionlint interrupted"),
            Self::Cleanup {
                previous, message, ..
            } => {
                if let Some(previous) = previous {
                    write!(f, "{previous}. ")?;
                }
                f.write_str(message)
            }
        }
    }
}
impl std::error::Error for LifecycleError {}

impl From<String> for LifecycleError {
    fn from(message: String) -> Self {
        Self::Operation(message)
    }
}
impl From<&str> for LifecycleError {
    fn from(message: &str) -> Self {
        Self::Operation(message.into())
    }
}

/// Lint the supplied bytes without loading workflows from disk. The host's
/// decoder runs once per successful client response, while the private volume
/// is owned. It returns the number of findings or its original bounded error.
/// The host may retain its native exception for propagation after cleanup.
/// Supply the empty private temporary directory the host created
/// and the remaining time from its original operation deadline. Cancellation
/// is polled on this thread. Once requested it must stay latched while resource
/// cleanup finishes under its independent deadlines.
///
/// ```no_run
/// use std::{path::Path, time::Duration};
/// use yaga_core::actionlint::{self, LifecycleError, LintResults, PreparedLint, VettedDocker};
///
/// fn check_loaded(repo: &Path, private_directory: &Path, client: &VettedDocker,
///                 snapshot: PreparedLint) -> Result<LintResults, LifecycleError> {
///     let mut results = Vec::new();
///     actionlint::lint_prepared(repo, private_directory, client, snapshot,
///         Duration::from_secs(300), || false, |path, response| {
///             let diagnostics = actionlint::diagnostics(response, path)?;
///             let count = diagnostics.len();
///             results.push((path.to_owned(), diagnostics));
///             Ok(count)
///         })?;
///     Ok(results)
/// }
/// ```
pub fn lint_prepared(
    repo: &Path,
    workspace: &Path,
    docker: &VettedDocker,
    prepared: PreparedLint,
    remaining: Duration,
    cancelled: impl Fn() -> bool,
    decode: impl FnMut(&str, &[u8]) -> Result<usize, String>,
) -> Result<(), LifecycleError> {
    let deadline = Instant::now() + remaining.min(Duration::from_secs(300));
    if !repo.is_absolute() {
        return Err("workflow repository path cannot be resolved".into());
    }
    let repo = absolute_path(repo).map_err(|_| "workflow repository path cannot be resolved")?;
    if !repo.is_dir() {
        return Err("workflow repository directory does not exist".into());
    }
    let docker = Docker::from_vetted(&repo, workspace, docker, &cancelled)?;
    let selected = prepared.selected;
    let snapshot = Snapshot::from_members(&selected, prepared.files, prepared.directories)?;
    let archive = snapshot.embedded_archive()?;
    docker.run(
        archive,
        &snapshot,
        selected.iter().map(String::as_str),
        deadline,
        decode,
    )?;
    if cancelled() {
        return Err(LifecycleError::Interrupted);
    }
    Ok(())
}
