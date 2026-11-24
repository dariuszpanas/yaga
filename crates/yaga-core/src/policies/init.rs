use super::{Error, Kind, TREE_KINDS, pretty_json, validate};
use crate::{
    commits::{ConfigStarter, MAX_CONFIG_BYTES},
    fs::{absolute_path, display_path, read_regular_prefix, resolve_user_path},
    text::safe_text,
};
use serde::Serialize;
use serde_json::json;
use std::{
    collections::{HashMap, HashSet},
    fs::{self, File},
    io::{ErrorKind, Write},
    path::{Path, PathBuf},
};
use tempfile::NamedTempFile;

fn repo_directory(repo: &Path) -> Result<PathBuf, Error> {
    let repo = resolve_user_path(repo).map_err(|_| {
        Error::Configuration(format!(
            "cannot resolve repository directory {}",
            safe_text(&display_path(repo), 300)
        ))
    })?;
    if !repo.is_dir() {
        return Err(Error::Configuration(format!(
            "repository directory does not exist or is not a directory: {}",
            repo.display()
        )));
    }
    Ok(repo)
}

fn exists(path: &Path) -> Result<bool, Error> {
    match path.symlink_metadata() {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(false),
        Err(_) => Err(Error::Configuration(format!(
            "cannot inspect policy target {}",
            path.display()
        ))),
    }
}

pub fn existing_policies(repo: &Path, kinds: &[Kind]) -> Result<HashMap<Kind, PathBuf>, Error> {
    let repo = repo_directory(repo)?;
    let mut result = HashMap::new();
    for &kind in kinds {
        let target = kind
            .default_path()
            .split('/')
            .fold(repo.clone(), |p, part| p.join(part));
        if exists(&target)? {
            result.insert(kind, target);
            continue;
        }
        if kind != Kind::Commit {
            continue;
        }
        let pyproject = repo.join("pyproject.toml");
        if !exists(&pyproject)? {
            continue;
        }
        let resolved = absolute_path(&pyproject)
            .map_err(|_| Error::Configuration("cannot resolve pyproject.toml".into()))?;
        if !resolved.starts_with(&repo) {
            return Err(Error::Configuration(format!(
                "configuration escapes repository {}: {}",
                repo.display(),
                pyproject.display()
            )));
        }
        let bytes = read_regular_prefix(&resolved, MAX_CONFIG_BYTES).map_err(|_| {
            Error::Configuration(format!("cannot read configuration {}", pyproject.display()))
        })?;
        if bytes.len() > MAX_CONFIG_BYTES {
            return Err(Error::Configuration(format!(
                "configuration exceeds {MAX_CONFIG_BYTES} bytes: {}",
                pyproject.display()
            )));
        }
        let text = std::str::from_utf8(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&bytes))
            .map_err(|_| {
                Error::Configuration(format!(
                    "configuration is not valid UTF-8: {}",
                    pyproject.display()
                ))
            })?;
        let doc: toml::Table = toml::from_str(text).map_err(|_| {
            Error::Configuration(format!("invalid TOML in {}", pyproject.display()))
        })?;
        if let Some(tool) = doc.get("tool") {
            let t = tool.as_table().ok_or_else(|| {
                Error::Configuration(format!("[tool] must be a table in {}", pyproject.display()))
            })?;
            if t.contains_key("yaga") {
                result.insert(kind, pyproject);
            }
        }
    }
    Ok(result)
}

/// Render a versioned policy starter, including the selected committed-tree providers.
pub fn render_starter(kind: Kind, active: &HashSet<Kind>) -> String {
    if kind == Kind::Repo {
        let mut providers = vec!["commit", "workflow", "workflow-security", "workflow-lint"];
        let (mut enabled, mut disabled) = (vec![], vec![]);
        for kind in TREE_KINDS {
            let line = format!("{}-policy = \"{}\"", kind.as_str(), kind.default_path());
            if active.contains(&kind) {
                providers.push(kind.as_str());
                enabled.push(line);
            } else {
                disabled.push(format!("# {line}"));
            }
        }
        let mut s = String::from("# YAGA repository check plan\nplan-version = 2\nchecks = [\n");
        s.push_str(
            &providers
                .iter()
                .map(|v| format!("    \"{v}\""))
                .collect::<Vec<_>>()
                .join(",\n"),
        );
        s.push_str("\n]\nworkflow-security-profile = \"recommended-v3\"\n");
        if !enabled.is_empty() {
            s.push('\n');
            s.push_str(&enabled.join("\n"));
            s.push('\n');
        }
        if !disabled.is_empty() {
            s.push_str("\n# Uncomment after initializing committed-tree policies:\n");
            s.push_str(&disabled.join("\n"));
            s.push('\n');
        }
        return s;
    }
    if kind == Kind::Commit {
        return format!(
            "# YAGA commit and pull-request title policy\n{}",
            std::str::from_utf8(ConfigStarter::RecommendedV1.bytes()).unwrap()
        );
    }
    match kind {
        Kind::Branch => include_str!("starters/branch.toml"),
        Kind::Change => include_str!("starters/change.toml"),
        Kind::Description => include_str!("starters/description.toml"),
        Kind::Mode => include_str!("starters/mode.toml"),
        Kind::Path => include_str!("starters/path.toml"),
        Kind::Size => include_str!("starters/size.toml"),
        Kind::Tree => include_str!("starters/tree.toml"),
        Kind::AgentReview => include_str!("starters/agent-review.toml"),
        Kind::Test => include_str!("starters/test.toml"),
        Kind::Triage => include_str!("starters/triage.toml"),
        Kind::Commit | Kind::Repo => unreachable!(),
    }
    .to_owned()
}

fn safe_target(repo: &Path, kind: Kind, create: bool) -> Result<PathBuf, Error> {
    let rel = Path::new(kind.default_path());
    let mut current = repo.to_path_buf();
    for part in rel.parent().unwrap().components() {
        current.push(part);
        if exists(&current)? {
            let invalid = || {
                Error::Configuration(format!(
                    "policy directory escapes repository or is not a directory: {}",
                    current.display()
                ))
            };
            let meta = current.symlink_metadata().map_err(|_| invalid())?;
            if meta.is_symlink() {
                return Err(Error::Configuration(format!(
                    "refusing to write through symlinked directory: {}",
                    current.display()
                )));
            }
            if !meta.is_dir()
                || !absolute_path(&current)
                    .map_err(|_| invalid())?
                    .starts_with(repo)
            {
                return Err(invalid());
            }
        } else if create {
            fs::create_dir(&current)
                .or_else(|e| {
                    if e.kind() == ErrorKind::AlreadyExists && current.is_dir() {
                        Ok(())
                    } else {
                        Err(e)
                    }
                })
                .map_err(|_| {
                    Error::Configuration(format!(
                        "cannot create policy directory {}",
                        current.display()
                    ))
                })?;
        }
    }
    let target = current.join(rel.file_name().expect("starter path has a filename"));
    if exists(&target)? {
        return Err(Error::Configuration(format!(
            "policy target already exists: {}",
            target.display()
        )));
    }
    Ok(target)
}

#[derive(Debug, Serialize)]
pub struct InitItem {
    pub kind: Kind,
    pub path: PathBuf,
    pub schema_version: i64,
    pub status: &'static str,
}
#[derive(Debug)]
pub struct InitReport {
    pub repository: PathBuf,
    pub dry_run: bool,
    pub items: Vec<InitItem>,
}

/// A selected policy and the bytes to stage, or an existing policy to skip.
#[derive(Debug)]
pub struct PreparedPolicy {
    pub item: InitItem,
    pub starter: Option<String>,
}

/// An existing policy prevents initialization. Retain the native paths so each
/// host can display them without losing bytes or unpaired UTF-16 code units.
#[derive(Debug)]
pub struct InitConflict {
    pub commit_repository: Option<PathBuf>,
    pub target: PathBuf,
}

impl std::fmt::Display for InitConflict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.commit_repository {
            Some(repo) => write!(
                f,
                "commit configuration already applies in {}: {}",
                repo.display(),
                self.target.display()
            ),
            None => write!(f, "policy target already exists: {}", self.target.display()),
        }
    }
}

/// A validated selection. An embedding can observe the existing policy files
/// with its own filesystem and TOML parser before preparing the same starters.
pub struct InitSelection<'a> {
    selected: &'a [Kind],
    inspect: Vec<Kind>,
}

impl<'a> InitSelection<'a> {
    pub fn new(kinds: &'a [Kind]) -> Result<Self, Error> {
        if kinds.is_empty() {
            return Err(Error::Input(
                "select at least one policy to initialize".into(),
            ));
        }
        let mut seen = HashSet::new();
        for &kind in kinds {
            if !seen.insert(kind) {
                return Err(Error::Input(format!(
                    "duplicate policy selection: {}",
                    kind.as_str()
                )));
            }
        }
        let mut inspect = kinds.to_vec();
        if seen.contains(&Kind::Repo) {
            inspect.extend(TREE_KINDS.into_iter().filter(|kind| !seen.contains(kind)));
        }
        Ok(Self {
            selected: kinds,
            inspect,
        })
    }

    pub fn inspection_kinds(&self) -> &[Kind] {
        &self.inspect
    }

    /// Prepare ordered, current-schema starters from the observed repository state.
    /// Filesystem consumers still verify target containment and publish without replacement.
    pub fn prepare(
        &self,
        repo: &Path,
        existing: &HashMap<Kind, PathBuf>,
        dry_run: bool,
        skip_existing: bool,
    ) -> impl Iterator<Item = Result<PreparedPolicy, InitConflict>> {
        let kinds = self.selected;
        let active = kinds
            .iter()
            .copied()
            .chain(existing.keys().copied())
            .collect();
        kinds.iter().map(move |&kind| {
            let (path, starter, status) = if let Some(path) = existing.get(&kind) {
                if !skip_existing {
                    return Err(InitConflict {
                        commit_repository: (kind == Kind::Commit
                            && path.ends_with("pyproject.toml"))
                        .then(|| repo.to_owned()),
                        target: path.clone(),
                    });
                }
                (path.clone(), None, "skipped")
            } else {
                let path = Path::new(kind.default_path())
                    .components()
                    .fold(repo.to_path_buf(), |p, part| p.join(part));
                (
                    path,
                    Some(render_starter(kind, &active)),
                    if dry_run { "dry-run" } else { "created" },
                )
            };
            Ok(PreparedPolicy {
                item: InitItem {
                    kind,
                    path,
                    schema_version: kind.current(),
                    status,
                },
                starter,
            })
        })
    }
}
impl InitReport {
    pub fn render(&self, as_json: bool) -> String {
        if as_json {
            let count = |status| self.items.iter().filter(|i| i.status == status).count();
            return pretty_json(
                &json!({"schema_version":1,"kind":"policy_init","repository":self.repository.to_string_lossy(),
                "dry_run":self.dry_run,"created":count("created"),"previewed":count("dry-run"),"skipped":count("skipped"),
                "policies":self.items.iter().map(|i|json!({"kind":i.kind,"path":i.path.to_string_lossy(),"schema_version":i.schema_version,"status":i.status})).collect::<Vec<_>>()}),
            );
        }
        let mut lines = vec![format!(
            "Repository: {} ({})",
            safe_text(&self.repository.to_string_lossy(), 300),
            if self.dry_run {
                "dry-run (no files created)"
            } else {
                "applied"
            }
        )];
        for i in &self.items {
            let badge = match i.status {
                "created" => "CREATED",
                "dry-run" => "PREVIEW",
                _ => "SKIPPED",
            };
            lines.push(format!(
                "{badge:<7}  {:<13}  v{}  {}",
                i.kind.as_str(),
                i.schema_version,
                safe_text(&i.path.to_string_lossy(), 300)
            ));
        }
        lines.join("\n")
    }
}

#[cfg(unix)]
fn same_file(file: &File, path: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    matches!((file.metadata(),path.symlink_metadata()),(Ok(a),Ok(b)) if a.dev()==b.dev() && a.ino()==b.ino())
}
#[cfg(windows)]
fn same_file(file: &File, path: &Path) -> bool {
    use same_file::Handle;
    matches!((file.try_clone().and_then(Handle::from_file),Handle::from_path(path)),(Ok(a),Ok(b)) if a==b)
}

pub fn initialize_policies(
    repo: &Path,
    kinds: &[Kind],
    dry_run: bool,
    skip_existing: bool,
) -> Result<InitReport, Error> {
    let selection = InitSelection::new(kinds)?;
    let repo = repo_directory(repo)?;
    let existing = existing_policies(&repo, selection.inspection_kinds())?;
    let mut items = vec![];
    let mut to_create = vec![];
    for row in selection.prepare(&repo, &existing, dry_run, skip_existing) {
        let PreparedPolicy { item, starter } =
            row.map_err(|e| Error::Configuration(e.to_string()))?;
        if let Some(text) = starter {
            let kind = item.kind;
            let path = safe_target(&repo, kind, false)?;
            validate(kind, text.as_bytes(), &path).map_err(Error::Configuration)?;
            to_create.push((kind, text));
        }
        items.push(item);
    }
    if !dry_run {
        let mut staged = Vec::<(Kind, NamedTempFile)>::new();
        for (kind, text) in to_create {
            let target = safe_target(&repo, kind, true)?;
            let error = |_| {
                Error::Configuration(format!(
                    "cannot stage temporary policy file for {}",
                    target.display()
                ))
            };
            let mut file = tempfile::Builder::new()
                .prefix(".yaga-policy.")
                .suffix(".tmp")
                .tempfile_in(target.parent().unwrap())
                .map_err(error)?;
            file.write_all(text.as_bytes()).map_err(error)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                file.as_file()
                    .set_permissions(fs::Permissions::from_mode(0o644))
                    .map_err(error)?;
            }
            file.as_file().sync_all().map_err(error)?;
            staged.push((kind, file));
        }
        let mut published = Vec::<(PathBuf, File)>::new();
        let result = (|| {
            for (kind, staging) in staged {
                let target = safe_target(&repo, kind, false)?;
                let file = staging.persist_noclobber(&target).map_err(|e| {
                    Error::Configuration(if e.error.kind() == ErrorKind::AlreadyExists {
                        format!("policy target already exists: {}", target.display())
                    } else {
                        format!("cannot publish policy file {}", target.display())
                    })
                })?;
                published.push((target.clone(), file));
                let raw = read_regular_prefix(&target, 128 * 1024).map_err(|_| {
                    Error::Configuration(format!(
                        "cannot read published policy {}",
                        target.display()
                    ))
                })?;
                validate(kind, &raw, &target).map_err(Error::Configuration)?;
            }
            Ok(())
        })();
        if let Err(e) = result {
            for (path, file) in &published {
                if same_file(file, path) {
                    let _ = fs::remove_file(path);
                }
            }
            return Err(e);
        }
    }
    Ok(InitReport {
        repository: repo,
        dry_run,
        items,
    })
}
