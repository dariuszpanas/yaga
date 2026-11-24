//! Local repository selection and bounded, shell-free Git operations.

mod metadata;
pub mod process;
mod selection;
#[cfg(test)]
mod tests;
pub use metadata::powershell_executable;
pub use selection::{
    ChangedPaths, CommittedTreeIdentity, GitTreeKind, parse_changed_paths, read_changed_paths,
    resolve_committed_tree, resolve_repository_tree,
};

use crate::text::safe_text;
use process::{ProcessError, run_bounded};
use std::fmt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::Duration;

const MAX_STDERR: usize = 64 * 1024;
const TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GitError {
    Input(String),
    Git(String),
    Interrupted,
}

impl GitError {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Input(_) => "input",
            Self::Git(_) | Self::Interrupted => "git",
        }
    }
}
impl fmt::Display for GitError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Input(message) | Self::Git(message) => f.write_str(message),
            Self::Interrupted => f.write_str("git command was interrupted"),
        }
    }
}
impl std::error::Error for GitError {}

pub fn safe_git_error(bytes: &[u8]) -> String {
    let text = safe_text(
        &String::from_utf8_lossy(&bytes[..bytes.len().min(1000)]),
        1000,
    );
    if text.is_empty() {
        "unknown git error".into()
    } else {
        text
    }
}

pub fn parse_object_id(bytes: &[u8], label: &str) -> Result<String, GitError> {
    let value = bytes
        .strip_suffix(b"\r\n")
        .or_else(|| bytes.strip_suffix(b"\n"))
        .or_else(|| bytes.strip_suffix(b"\r"))
        .unwrap_or(bytes);
    if !matches!(value.len(), 40 | 64)
        || !value
            .iter()
            .all(|&b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(GitError::Git(format!(
            "git returned a malformed {label} identity"
        )));
    }
    Ok(String::from_utf8(value.to_vec()).expect("lowercase ASCII hex is UTF-8"))
}

#[derive(Clone, Debug)]
pub struct Repository {
    directory: PathBuf,
    git: PathBuf,
}

/// Resolve an external tool outside all enclosing repository trust boundaries.
pub fn external_executable(directory: &Path, name: &str) -> Result<PathBuf, GitError> {
    metadata::find_tool(directory, name)
}

impl Repository {
    pub fn open(repo: &Path) -> Result<Self, GitError> {
        let error = || {
            GitError::Input(format!(
                "repository path cannot be resolved safely: {}",
                safe_text(&crate::fs::display_path(repo), 300)
            ))
        };
        let directory = crate::fs::resolve_user_path(repo).map_err(|_| error())?;
        if !directory.is_dir() {
            return Err(error());
        }
        let git = metadata::find_tool(&directory, "git")?;
        Ok(Self { directory, git })
    }

    pub fn directory(&self) -> &Path {
        &self.directory
    }

    /// Attach the caller's already discovered executable to a repository. Both
    /// the original spelling and resolved binary must clear the Git metadata
    /// and enclosing worktree checks used for normal PATH-based discovery.
    pub fn with_executable(repo: &Path, executable: &Path) -> Result<Self, GitError> {
        let directory = crate::fs::resolve_user_path(repo)
            .map_err(|_| GitError::Input("repository path cannot be resolved safely".into()))?;
        if !directory.is_dir() {
            return Err(GitError::Input(
                "repository directory does not exist".into(),
            ));
        }
        let git = metadata::selected_git(&directory, executable)?;
        Ok(Self { directory, git })
    }

    pub fn find_external_executable(&self, name: &str) -> Result<PathBuf, GitError> {
        metadata::find_tool(&self.directory, name)
    }

    fn command(&self) -> Result<Command, GitError> {
        #[cfg(windows)]
        {
            let current = self.git.canonicalize().map_err(|_| {
                GitError::Git("git executable path cannot be resolved safely".into())
            })?;
            metadata::require_windows_binary(&current, "git")?;
            if current != self.git {
                return Err(GitError::Git(
                    "git executable path changed after repository selection".into(),
                ));
            }
        }
        let mut cmd = Command::new(&self.git);
        cmd.args(["--no-pager", "--no-lazy-fetch", "-C"])
            .arg(&self.directory)
            .env_clear();
        for name in ["SYSTEMROOT", "WINDIR"] {
            if let Some(value) = std::env::var_os(name) {
                cmd.env(name, value);
            }
        }
        let null = if cfg!(windows) { "nul" } else { "/dev/null" };
        cmd.envs([
            ("GCM_INTERACTIVE", "Never"),
            ("GIT_ATTR_NOSYSTEM", "1"),
            ("GIT_CONFIG_GLOBAL", null),
            ("GIT_CONFIG_NOSYSTEM", "1"),
            ("GIT_CONFIG_SYSTEM", null),
            ("GIT_NO_LAZY_FETCH", "1"),
            ("GIT_NO_REPLACE_OBJECTS", "1"),
            ("GIT_OPTIONAL_LOCKS", "0"),
            ("GIT_TERMINAL_PROMPT", "0"),
            ("LC_ALL", "C"),
        ]);
        Ok(cmd)
    }

    /// Run a provider-selected Git operation. Callers validate revisions and choose fixed arguments.
    pub fn run(&self, args: &[&str], stdout_limit: usize) -> Result<Output, GitError> {
        self.run_with_timeout(args, stdout_limit, TIMEOUT)
    }

    pub(crate) fn run_with_timeout(
        &self,
        args: &[&str],
        stdout_limit: usize,
        timeout: Duration,
    ) -> Result<Output, GitError> {
        self.run_interruptible(args, stdout_limit, MAX_STDERR, timeout, || false)
    }

    pub fn run_interruptible(
        &self,
        args: &[&str],
        stdout_limit: usize,
        stderr_limit: usize,
        timeout: Duration,
        interrupted: impl FnMut() -> bool,
    ) -> Result<Output, GitError> {
        if args.iter().any(|s| s.contains('\0')) {
            return Err(GitError::Git(
                "git arguments must not contain NUL bytes".into(),
            ));
        }
        let mut command = self.command()?;
        command.args(args);
        let timeout = timeout.min(TIMEOUT);
        let stderr_limit = stderr_limit.min(MAX_STDERR);
        process::run_interruptible(
            command,
            None,
            stdout_limit,
            stderr_limit,
            timeout,
            interrupted,
        )
        .map_err(|error| {
            if matches!(&error, ProcessError::Io(e) if e.kind() == std::io::ErrorKind::Interrupted)
            {
                return GitError::Interrupted;
            }
            GitError::Git(match error {
                ProcessError::Timeout => format!(
                    "git command exceeded the hard {}-second limit",
                    timeout.as_secs_f64()
                ),
                ProcessError::StdoutLimit => {
                    format!("git output exceeds the hard {stdout_limit}-byte limit")
                }
                ProcessError::StderrLimit => {
                    format!("git error output exceeds the hard {stderr_limit}-byte limit")
                }
                ProcessError::Io(error) => format!(
                    "could not execute git: {}",
                    safe_text(&error.to_string(), 300)
                ),
            })
        })
    }

    pub fn strip_editor_comments(
        &self,
        bytes: Vec<u8>,
        maximum: usize,
    ) -> Result<Vec<u8>, GitError> {
        let mut cmd = self.command()?;
        for name in [
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
        ] {
            cmd.env_remove(name);
        }
        for name in [
            "HOME",
            "XDG_CONFIG_HOME",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
        ] {
            if let Some(value) = std::env::var_os(name) {
                cmd.env(name, value);
            }
        }
        cmd.args(["stripspace", "--strip-comments"]);
        let output = run_bounded(cmd, Some(bytes), maximum, MAX_STDERR, TIMEOUT).map_err(|e| {
            GitError::Git(
                if matches!(e, ProcessError::Io(_)) {
                    "Git could not clean the editor message"
                } else {
                    "Git editor cleanup exceeded its bounded execution limits"
                }
                .into(),
            )
        })?;
        if !output.status.success() {
            return Err(GitError::Git(format!(
                "Git editor cleanup failed: {}",
                safe_git_error(&output.stderr)
            )));
        }
        Ok(output.stdout)
    }

    pub fn require_no_legacy_grafts(&self, message: &str) -> Result<(), GitError> {
        let result = self.run(&["rev-parse", "--git-common-dir"], 32 * 1024)?;
        if !result.status.success() {
            return Err(GitError::Git(format!(
                "git could not locate common history metadata: {}",
                safe_git_error(&result.stderr)
            )));
        }
        let invalid = || GitError::Git("git returned malformed common history metadata".into());
        let decoded = std::str::from_utf8(&result.stdout).map_err(|_| invalid())?;
        let line = decoded
            .strip_suffix("\r\n")
            .or_else(|| decoded.strip_suffix('\n'))
            .unwrap_or(decoded);
        if line.is_empty()
            || line.contains([
                '\0', '\r', '\n', '\x0b', '\x0c', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}',
                '\u{2028}', '\u{2029}',
            ])
        {
            return Err(invalid());
        }
        let common = self
            .directory
            .join(line)
            .canonicalize()
            .map_err(|_| invalid())?;
        if !common.is_dir() {
            return Err(invalid());
        }
        match common.join("info/grafts").symlink_metadata() {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Ok(m) if m.is_file() && m.len() == 0 => Ok(()),
            Ok(m) if m.is_file() => Err(GitError::Git(message.into())),
            _ => Err(GitError::Git(
                "git history overlay metadata cannot be validated safely".into(),
            )),
        }
    }

    pub fn require_complete_history(&self, shallow: &str, grafts: &str) -> Result<(), GitError> {
        self.require_no_legacy_grafts(grafts)?;
        let result = self.run(&["rev-parse", "--is-shallow-repository"], 16)?;
        if !result.status.success() {
            return Err(GitError::Git(format!(
                "git could not determine whether history is complete: {}",
                safe_git_error(&result.stderr)
            )));
        }
        match result.stdout.trim_ascii() {
            b"false" => Ok(()),
            b"true" => Err(GitError::Git(shallow.into())),
            _ => Err(GitError::Git(
                "git returned an invalid shallow-repository state".into(),
            )),
        }
    }
}
