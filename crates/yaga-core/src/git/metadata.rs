use super::GitError;
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};

const METADATA_BYTES: usize = 32 * 1024;
const ALTERNATES: usize = 128;

fn exists(path: &Path) -> Result<bool, GitError> {
    match path.symlink_metadata() {
        Ok(_) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(GitError::Input(
            "repository boundary cannot be inspected safely".into(),
        )),
    }
}

fn directory(path: &Path, label: &str) -> Result<PathBuf, GitError> {
    let canonical = path
        .canonicalize()
        .map_err(|_| GitError::Input(format!("{label} cannot be resolved safely")))?;
    if !canonical.is_dir() {
        return Err(GitError::Input(format!(
            "{label} must resolve to a directory"
        )));
    }
    Ok(canonical)
}

fn read_metadata(path: &Path, label: &str) -> Result<Vec<u8>, GitError> {
    let unavailable = |_| GitError::Input(format!("{label} cannot be read safely"));
    let before = path.symlink_metadata().map_err(unavailable)?;
    if !before.is_file() {
        return Err(GitError::Input(format!("{label} must be a regular file")));
    }
    let too_big = || GitError::Input(format!("{label} exceeds {METADATA_BYTES} bytes"));
    if before.len() > METADATA_BYTES as u64 {
        return Err(too_big());
    }
    let file = crate::fs::open_regular(path, &before).map_err(unavailable)?;
    let mut raw = Vec::new();
    file.take(METADATA_BYTES as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(unavailable)?;
    if raw.len() > METADATA_BYTES {
        return Err(too_big());
    }
    Ok(raw)
}

fn path_bytes(value: &[u8]) -> Result<OsString, GitError> {
    if value.is_empty() || value.iter().any(|b| matches!(b, 0 | b'\r' | b'\n')) {
        return Err(GitError::Input(
            "repository metadata must contain one bounded path".into(),
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        Ok(std::ffi::OsStr::from_bytes(value).to_owned())
    }
    #[cfg(windows)]
    {
        std::str::from_utf8(value)
            .map(Into::into)
            .map_err(|_| GitError::Input("repository metadata contains an invalid path".into()))
    }
}

fn strip_eol(value: &[u8]) -> &[u8] {
    value
        .strip_suffix(b"\r\n")
        .or_else(|| value.strip_suffix(b"\n"))
        .unwrap_or(value)
}

fn read_directory(file: &Path, prefix: &[u8], label: &str) -> Result<PathBuf, GitError> {
    let raw = read_metadata(file, label)?;
    let value = strip_eol(&raw)
        .strip_prefix(prefix)
        .ok_or_else(|| GitError::Input(format!("{label} contains an invalid path prefix")))?;
    directory(&file.parent().unwrap().join(path_bytes(value)?), label)
}

fn object_boundaries(initial: PathBuf, boundaries: &mut HashSet<PathBuf>) -> Result<(), GitError> {
    let mut seen = HashSet::new();
    let mut pending = vec![initial];
    let overflow = || {
        GitError::Input(format!(
            "repository object alternates exceed the hard {ALTERNATES}-directory limit"
        ))
    };
    while let Some(next) = pending.pop() {
        let path = directory(&next, "repository object directory")?;
        if !seen.insert(path.clone()) {
            continue;
        }
        if seen.len() > ALTERNATES {
            return Err(overflow());
        }
        boundaries.insert(path.clone());
        let alternate = path.join("info/alternates");
        if !exists(&alternate)? {
            continue;
        }
        let raw = read_metadata(&alternate, "repository object alternates")?;
        if raw.is_empty() {
            continue;
        }
        for (i, line) in raw
            .strip_suffix(b"\n")
            .unwrap_or(&raw)
            .split(|&b| b == b'\n')
            .enumerate()
        {
            if i >= ALTERNATES {
                return Err(overflow());
            }
            let line = line.strip_suffix(b"\r").unwrap_or(line);
            // Git's quoted-path decoding differs from raw filesystem spelling.
            // An unrecognized alternate cannot safely define an executable boundary.
            if line.starts_with(b"\"") {
                return Err(GitError::Input(
                    "repository object alternates contain an unsupported quoted path".into(),
                ));
            }
            pending.push(path.join(path_bytes(line)?));
        }
    }
    Ok(())
}

fn add_git_directory(path: PathBuf, boundaries: &mut HashSet<PathBuf>) -> Result<(), GitError> {
    let marker = path.join("commondir");
    let common = if exists(&marker)? {
        read_directory(&marker, b"", "repository commondir file")?
    } else {
        path.clone()
    };
    object_boundaries(common.join("objects"), boundaries)?;
    boundaries.extend([path, common]);
    Ok(())
}

pub(super) fn boundaries(repo: &Path) -> Result<HashSet<PathBuf>, GitError> {
    let repo = directory(repo, "repository directory")?;
    let mut boundaries = enclosing_boundaries(&repo)?;
    boundaries.insert(repo);
    Ok(boundaries)
}

fn enclosing_boundaries(repo: &Path) -> Result<HashSet<PathBuf>, GitError> {
    let mut boundaries = HashSet::new();
    for candidate in repo.ancestors() {
        let marker = candidate.join(".git");
        if exists(&marker)? {
            boundaries.insert(candidate.to_owned());
            let resolved = marker.canonicalize().map_err(|_| {
                GitError::Input("repository .git marker cannot be resolved safely".into())
            })?;
            let git_dir = if resolved.is_dir() {
                resolved
            } else {
                read_directory(&marker, b"gitdir: ", "repository .git file")?
            };
            add_git_directory(git_dir, &mut boundaries)?;
        }
        if exists(&candidate.join("HEAD"))? && exists(&candidate.join("objects"))? {
            add_git_directory(candidate.to_owned(), &mut boundaries)?;
        }
    }
    Ok(boundaries)
}

fn executable(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(windows)]
    {
        metadata.is_file()
    }
}

#[cfg(windows)]
pub(super) fn require_windows_binary(path: &Path, name: &str) -> Result<(), GitError> {
    let filename = path.file_name().unwrap_or_default().to_string_lossy();
    let filename = filename.trim_end_matches([' ', '.']);
    let extension = filename
        .rsplit_once('.')
        .map(|(_, suffix)| suffix)
        .unwrap_or_default();
    if extension.eq_ignore_ascii_case("bat") || extension.eq_ignore_ascii_case("cmd") {
        return Err(GitError::Git(format!(
            "{name} executable must not be a Windows command script (.bat or .cmd)"
        )));
    }
    if !extension.eq_ignore_ascii_case("exe") && !extension.eq_ignore_ascii_case("com") {
        return Err(GitError::Git(format!(
            "{name} executable must resolve to an executable binary"
        )));
    }
    Ok(())
}

fn path_candidate(name: &str) -> Result<PathBuf, GitError> {
    if name.is_empty()
        || !name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
    {
        return Err(GitError::Git(
            "executable name must be a fixed lowercase command name".into(),
        ));
    }
    let path = std::env::var_os("PATH").unwrap_or_default();
    let candidates = std::env::split_paths(&path);
    #[cfg(unix)]
    let mut candidates = candidates.map(|dir| dir.join(name));
    #[cfg(windows)]
    let extensions = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
    #[cfg(windows)]
    let names: Vec<_> = extensions
        .split(';')
        .filter_map(|extension| extension.strip_prefix('.'))
        .filter(|ext| !ext.is_empty() && ext.bytes().all(|b| b.is_ascii_alphanumeric()))
        .map(|ext| format!("{name}.{ext}"))
        .collect();
    #[cfg(windows)]
    let mut candidates = candidates.flat_map(|dir| names.iter().map(move |n| dir.join(n)));
    candidates
        .find(|p| executable(p))
        .ok_or_else(|| GitError::Git(format!("{name} executable was not found")))
}

pub(super) fn find_tool(repo: &Path, name: &str) -> Result<PathBuf, GitError> {
    let boundaries = boundaries(repo)?;
    verify_candidate(&path_candidate(name)?, name, &boundaries)
}

/// A local shell installation can be under the current non-repository home.
/// Keep actual Git boundaries and reject a command found directly in the CWD.
pub fn powershell_executable(cwd: &Path, name: &str) -> Result<PathBuf, GitError> {
    if !matches!(name, "powershell" | "pwsh") {
        return Err(GitError::Input("unsupported PowerShell executable".into()));
    }
    let cwd = directory(cwd, "current directory")?;
    let boundaries = enclosing_boundaries(&cwd)?;
    let candidate = path_candidate(name)?;
    let resolved = verify_candidate(&candidate, name, &boundaries)?;
    if candidate
        .parent()
        .and_then(|parent| parent.canonicalize().ok())
        .as_ref()
        == Some(&cwd)
        || resolved.parent() == Some(cwd.as_path())
    {
        return Err(GitError::Input(
            "shell executable must be outside the current directory".into(),
        ));
    }
    Ok(resolved)
}

pub(super) fn selected_git(repo: &Path, candidate: &Path) -> Result<PathBuf, GitError> {
    let boundaries = boundaries(repo)?;
    if !executable(candidate) {
        return Err(GitError::Git(
            "git executable is not a regular executable file".into(),
        ));
    }
    verify_candidate(candidate, "git", &boundaries)
}

fn verify_candidate(
    candidate: &Path,
    name: &str,
    boundaries: &HashSet<PathBuf>,
) -> Result<PathBuf, GitError> {
    if !candidate.is_absolute() {
        return Err(GitError::Git(format!(
            "{name} executable path must be absolute"
        )));
    }
    let resolved = candidate
        .canonicalize()
        .map_err(|_| GitError::Git(format!("{name} executable path cannot be resolved safely")))?;
    // Include enclosing directories of the discovered spelling: a symlink inside
    // an untrusted worktree may resolve to a binary outside it.
    for parent in candidate
        .ancestors()
        .skip(1)
        .chain(resolved.ancestors().skip(1))
    {
        let parent = parent.canonicalize().map_err(|_| {
            GitError::Git(format!("{name} executable path cannot be resolved safely"))
        })?;
        if boundaries.contains(&parent) {
            return Err(GitError::Git(format!(
                "{name} executable must not be discovered inside the repository"
            )));
        }
    }
    #[cfg(windows)]
    for file in [candidate, resolved.as_path()] {
        require_windows_binary(file, name)?;
    }
    Ok(resolved)
}
