//! Shared filesystem handling for explicit local policy inputs.

pub(crate) mod user_path;
pub use user_path::resolve_user_path;
#[cfg(windows)]
pub use user_path::windows_cli_home;

use std::fs::{File, Metadata, OpenOptions};
use std::io::{self, Read, Result as IoResult};
use std::path::{Path, PathBuf};

/// Spell a caller-supplied path as pathlib would, without resolving the filesystem.
pub fn display_path(path: &Path) -> String {
    let normalized: PathBuf = path
        .components()
        .filter(|c| !matches!(c, std::path::Component::CurDir))
        .collect();
    if normalized.as_os_str().is_empty() {
        return ".".into();
    }
    #[cfg(unix)]
    if path.as_os_str().as_encoded_bytes().starts_with(b"//")
        && !path.as_os_str().as_encoded_bytes().starts_with(b"///")
    {
        return format!("/{}", normalized.display());
    }
    normalized.display().to_string()
}

pub fn absolute_path(path: &Path) -> IoResult<PathBuf> {
    let expanded = if let Ok(rest) = path.strip_prefix("~") {
        std::env::home_dir()
            .ok_or_else(|| std::io::Error::other("missing home directory"))?
            .join(rest)
    } else {
        path.to_owned()
    };
    soft_canonicalize::soft_canonicalize(expanded)
}

#[cfg(unix)]
fn open_existing_regular(
    path: &Path,
    before: &Metadata,
    options: &mut OpenOptions,
) -> io::Result<File> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let flags = rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::NONBLOCK;
    let file = options.custom_flags(flags.bits() as i32).open(path)?;
    let opened = file.metadata()?;
    if !opened.is_file() || (opened.dev(), opened.ino()) != (before.dev(), before.ino()) {
        return Err(io::Error::other("metadata file changed while opening"));
    }
    Ok(file)
}

#[cfg(windows)]
fn open_existing_regular(path: &Path, _: &Metadata, options: &mut OpenOptions) -> io::Result<File> {
    use same_file::Handle;
    use std::os::windows::fs::OpenOptionsExt;
    // Open the entry itself, including when replaced by a reparse point.
    const OPEN_REPARSE_POINT: u32 = 0x0020_0000;
    options.custom_flags(OPEN_REPARSE_POINT);
    let inspected = options.open(path)?;
    if !inspected.metadata()?.is_file() {
        return Err(io::Error::other("metadata must be regular"));
    }
    let first = Handle::from_file(inspected)?;
    let file = options.open(path)?;
    if !file.metadata()?.is_file() || Handle::from_file(file.try_clone()?)? != first {
        return Err(io::Error::other("metadata file changed while opening"));
    }
    Ok(file)
}

pub(crate) fn open_regular(path: &Path, before: &Metadata) -> io::Result<File> {
    open_existing_regular(path, before, OpenOptions::new().read(true))
}

/// Open the physical target for an in-place write without truncating it before validation.
pub fn open_regular_for_write(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true);
    match path.symlink_metadata() {
        Ok(before) if before.is_file() => open_existing_regular(path, &before, &mut options),
        Ok(_) => Err(io::Error::other("target must be a regular file")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            options.create_new(true).open(path)
        }
        Err(error) => Err(error),
    }
}

fn checked_file(path: &Path) -> io::Result<File> {
    let before = path.symlink_metadata()?;
    if !before.is_file() {
        return Err(io::Error::other("input must be a regular file"));
    }
    open_regular(path, &before)
}

/// Hash one bounded regular file without following links or buffering the file in memory.
pub fn sha256_regular_file(path: &Path, maximum: u64) -> io::Result<String> {
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    let size = io::copy(
        &mut checked_file(path)?.take(maximum.saturating_add(1)),
        &mut digest,
    )?;
    if size > maximum {
        return Err(io::Error::other("file exceeds the byte limit"));
    }
    Ok(format!("{:x}", digest.finalize()))
}

/// Read up to maximum + 1 bytes, checking the file identity after a nonblocking
/// open. Resolve an allowed symlink before calling. Callers own overflow errors.
pub fn read_regular_prefix(path: &Path, maximum: usize) -> io::Result<Vec<u8>> {
    let file = checked_file(path)?;
    let mut result = Vec::new();
    file.take((maximum as u64).saturating_add(1))
        .read_to_end(&mut result)?;
    Ok(result)
}
