#[cfg(feature = "process")]
use super::{LoadedConfig, load_config};
#[cfg(feature = "process")]
use crate::{fs::resolve_user_path, text::safe_text};
#[cfg(feature = "process")]
use std::{
    fs,
    io::{ErrorKind, Write},
    path::Path,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConfigStarter {
    RecommendedV1,
    TitleV1,
    CompleteMessageV1,
    PlainV1,
}

impl ConfigStarter {
    pub fn bytes(self) -> &'static [u8] {
        match self {
            Self::RecommendedV1 => include_bytes!("starters/recommended-v1.toml"),
            Self::TitleV1 => include_bytes!("starters/title-v1.toml"),
            Self::CompleteMessageV1 => include_bytes!("starters/complete-message-v1.toml"),
            Self::PlainV1 => include_bytes!("starters/plain-v1.toml"),
        }
    }
}

#[cfg(feature = "process")]
fn display(p: &Path) -> String {
    safe_text(&p.to_string_lossy(), 1000)
}

#[cfg(feature = "process")]
fn no_applied_config(repo: &Path) -> Result<(), String> {
    if let Some(existing) = load_config(None, repo, None)?.path {
        return Err(format!(
            "configuration already applies to {}: {}",
            display(repo),
            display(&existing)
        ));
    }
    Ok(())
}

#[cfg(feature = "process")]
fn require_absent(target: &Path) -> Result<(), String> {
    match fs::symlink_metadata(target) {
        Ok(_) => Err(format!(
            "configuration target already exists: {}",
            display(target)
        )),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
        Err(_) => Err(format!(
            "cannot inspect configuration target {}",
            display(target)
        )),
    }
}

/// Validate a frozen starter using the normal loader, then atomically publish
/// the complete same-directory file if no policy already applies.
#[cfg(feature = "process")]
pub fn initialize_config(
    repo: &Path,
    dry_run: bool,
    starter: ConfigStarter,
) -> Result<LoadedConfig, String> {
    let repo = resolve_user_path(repo).map_err(|error| {
        format!(
            "cannot resolve configuration repository directory {}: {error}",
            safe_text(&crate::fs::display_path(repo), 1000)
        )
    })?;
    if !repo.is_dir() {
        return Err(format!(
            "configuration repository directory does not exist or is not a directory: {}",
            display(&repo)
        ));
    }
    no_applied_config(&repo)?;
    let target = repo.join(".yaga.toml");
    require_absent(&target)?;
    let prepare_error = |_| format!("cannot prepare configuration {}", display(&target));
    let mut temp = tempfile::Builder::new()
        .prefix(".yaga.toml.")
        .suffix(".tmp")
        .tempfile_in(&repo)
        .map_err(prepare_error)?;
    temp.write_all(starter.bytes()).map_err(prepare_error)?;
    temp.flush().map_err(prepare_error)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        temp.as_file()
            .set_permissions(fs::Permissions::from_mode(0o644))
            .map_err(prepare_error)?;
    }
    temp.as_file().sync_all().map_err(prepare_error)?;
    let mut loaded = load_config(Some(temp.path()), &repo, None)?;
    no_applied_config(&repo)?;
    if dry_run {
        loaded.path = Some(target);
        return Ok(loaded);
    }
    if temp.persist_noclobber(&target).is_err() {
        require_absent(&target)?;
        return Err(format!("cannot publish configuration {}", display(&target)));
    }
    load_config(Some(&target), &repo, None)
}
