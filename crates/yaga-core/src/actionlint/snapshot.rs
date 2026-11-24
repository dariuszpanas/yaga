use crate::fs::{absolute_path, read_regular_prefix};
use crate::workflows::{
    ParsedInput, Reference, ReferenceContext, WorkflowInput, parse_action_manifest,
    parse_for_actionlint,
};
use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet, HashSet, VecDeque};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

const MAX_FILE: usize = 1024 * 1024;
const MAX_FILES: usize = 256;
const MAX_BYTES: usize = 16 * MAX_FILE;
const MAX_ENTRIES: usize = 20_000;
const MAX_ARCHIVE: usize = 20 * MAX_FILE;
const CONFIG: [&str; 2] = [".github/actionlint.yaml", ".github/actionlint.yml"];

pub(super) struct Snapshot {
    pub files: BTreeMap<String, Vec<u8>>,
    directories: BTreeSet<String>,
    bytes: usize,
}

fn path_ok(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 4096
        && !s.contains(['\0', '\\'])
        && s.as_bytes().get(1) != Some(&b':')
        && s.split('/').count() <= 64
        && !s.split('/').any(|part| matches!(part, "" | "." | ".."))
}
fn checked_relative(s: &str) -> Result<(), String> {
    if path_ok(s) {
        Ok(())
    } else {
        Err("workflow path is not a bounded repository-relative path".into())
    }
}

fn join_relative<'a>(mut parts: Vec<&'a str>, value: &'a str) -> Option<String> {
    for part in value.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop()?;
            }
            _ => parts.push(part),
        }
    }
    Some(parts.join("/"))
}
fn dependency(reference: &Reference) -> Result<Option<String>, String> {
    let value = &*reference.value;
    let Some(target) = value
        .strip_prefix("./")
        .or_else(|| value.strip_prefix("$/"))
    else {
        return Ok(None);
    };
    let job = matches!(reference.context, ReferenceContext::Job);
    if target.contains(['\0', '\\'])
        || value
            .find("${{")
            .zip(value.find("}}"))
            .is_some_and(|(a, b)| a < b)
        || (job && (target.is_empty() || target.contains('@')))
        || target.as_bytes().get(1) == Some(&b':')
        || target.starts_with("//")
    {
        return Ok(None);
    }
    if target.len() > 4096 || target.split('/').count() > 64 {
        return Err("actionlint local reference path exceeds its bounded support limit".into());
    }
    let Some(relative) = join_relative(Vec::new(), target) else {
        return Ok(None);
    };
    if relative.is_empty() {
        return Ok((!job).then_some(relative));
    }
    checked_relative(&relative)?;
    Ok(Some(relative))
}

fn runtime_path(manifest: &str, field: &str, value: &str) -> Result<Option<String>, String> {
    if value.is_empty()
        || value.starts_with('/')
        || value.contains('\\')
        || (field == "image"
            && ["docker://", "gcr.io/", "pkg.dev/", "ghcr.io/", "docker.io/"]
                .iter()
                .any(|p| value.starts_with(p)))
    {
        return Ok(None);
    }
    let mut parts = manifest.split('/').collect::<Vec<_>>();
    parts.pop();
    let Some(path) = join_relative(parts, value).filter(|p| !p.is_empty()) else {
        return Ok(None);
    };
    checked_relative(&path)?;
    Ok(Some(path))
}

pub struct PreparedWorkflow<'a> {
    pub content: Cow<'a, [u8]>,
    pub dependencies: Vec<(ReferenceContext, String)>,
}

/// Resolve local uses and rewrite only marked literal self-references.
/// Borrowed inputs remain borrowed unless a byte changes. Owned inputs are edited in place.
pub fn prepare_workflow<'a>(
    mut content: Cow<'a, [u8]>,
    parsed: &ParsedInput,
) -> Result<PreparedWorkflow<'a>, String> {
    let bom = if content.starts_with(b"\xef\xbb\xbf") {
        3
    } else {
        0
    };
    let mut edits = BTreeMap::new();
    let mut dependencies = Vec::new();
    for reference in &parsed.references {
        let Some(dep) = dependency(reference)? else {
            continue;
        };
        if reference.value.starts_with("$/") {
            let (start, end) = reference
                .source_start
                .zip(reference.source_end)
                .ok_or("actionlint self-reference source location is unavailable")?;
            let source = content[bom..]
                .get(start..end)
                .ok_or("actionlint self-reference source location is invalid")?;
            let source = std::str::from_utf8(source)
                .map_err(|_| "actionlint self-reference source location is invalid")?;
            let mut indices = source.match_indices(&*reference.value);
            let offset = indices
                .next()
                .ok_or("actionlint self-reference cannot be normalized safely")?
                .0;
            if indices.next().is_some() {
                return Err("actionlint self-reference cannot be normalized safely".into());
            }
            if let Some(previous) = edits.insert((start, end), bom + start + offset)
                && previous != bom + start + offset
            {
                return Err("actionlint self-reference source location is ambiguous".into());
            }
        }
        dependencies.push((reference.context, dep));
    }
    if !edits.is_empty() {
        let bytes = content.to_mut();
        for offset in edits.into_values() {
            bytes[offset] = b'.';
        }
    }
    Ok(PreparedWorkflow {
        content,
        dependencies,
    })
}

fn support(repo: &Path, path: &str) -> Result<Option<PathBuf>, String> {
    checked_relative(path)?;
    let candidate = repo.join(path);
    let fail = || "actionlint support file cannot be read safely".to_owned();
    match candidate.symlink_metadata() {
        Ok(_) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(fail()),
    }
    let resolved = absolute_path(&candidate).map_err(|_| fail())?;
    if !resolved.starts_with(repo) || !resolved.exists() {
        return Err(fail());
    }
    Ok(Some(resolved))
}

impl Snapshot {
    pub(super) fn from_members(
        selected: &[String],
        files: BTreeMap<String, Vec<u8>>,
        directories: BTreeSet<String>,
    ) -> Result<Self, String> {
        if selected.is_empty() || selected.len() > 128 {
            return Err("preloaded workflow inputs must contain 1 through 128 files".into());
        }
        if files.len().saturating_add(directories.len()) > MAX_FILES {
            return Err("actionlint workspace exceeds its file limit".into());
        }
        let mut seen = HashSet::with_capacity(selected.len());
        let mut selected_bytes = 0;
        for path in selected {
            checked_relative(path)?;
            if !Path::new(path).extension().is_some_and(|ext| {
                ext.eq_ignore_ascii_case("yaml") || ext.eq_ignore_ascii_case("yml")
            }) {
                return Err("preloaded workflow files must use a .yml or .yaml extension".into());
            }
            if !seen.insert(path) {
                return Err("preloaded workflow inputs contain a duplicate path".into());
            }
            let content = files
                .get(path)
                .ok_or("selected workflow is missing from the actionlint workspace")?;
            if content.len() > MAX_FILE {
                return Err("preloaded workflow input exceeds the hard 1048576-byte limit".into());
            }
            selected_bytes += content.len();
            if selected_bytes > 8 * MAX_FILE {
                return Err(
                    "preloaded workflow inputs exceed the hard 8388608-byte total limit".into(),
                );
            }
        }
        let mut bytes = 0;
        for (path, content) in &files {
            checked_relative(path)?;
            if content.len() > MAX_FILE {
                return Err("actionlint support file exceeds its byte limit".into());
            }
            bytes += content.len();
            if bytes > MAX_BYTES {
                return Err("actionlint workspace exceeds its byte limit".into());
            }
        }
        for path in &directories {
            checked_relative(path)?;
        }
        if CONFIG.iter().all(|p| files.contains_key(*p)) {
            return Err("repository contains both supported native actionlint config files".into());
        }
        Ok(Self {
            files,
            directories,
            bytes,
        })
    }

    fn insert(&mut self, name: String, bytes: Vec<u8>) -> Result<(), String> {
        if self.files.len() + self.directories.len() >= MAX_FILES {
            return Err("actionlint workspace exceeds its file limit".into());
        }
        self.bytes += bytes.len();
        if self.bytes > MAX_BYTES {
            return Err("actionlint workspace exceeds its byte limit".into());
        }
        self.files.insert(name, bytes);
        Ok(())
    }
    fn file(&mut self, repo: &Path, name: &str) -> Result<bool, String> {
        if self.files.contains_key(name) {
            return Ok(false);
        }
        let Some(resolved) = support(repo, name)? else {
            return Ok(false);
        };
        let bytes = read_regular_prefix(&resolved, MAX_FILE)
            .map_err(|_| "actionlint support file cannot be read safely")?;
        if bytes.len() > MAX_FILE {
            return Err("actionlint support file exceeds its byte limit".into());
        }
        self.insert(name.into(), bytes)?;
        Ok(true)
    }
    fn presence(&mut self, repo: &Path, name: String) -> Result<(), String> {
        if self.files.contains_key(&name) || self.directories.contains(&name) {
            return Ok(());
        }
        let Ok(Some(path)) = support(repo, &name) else {
            return Ok(());
        };
        if path.is_dir() {
            if self.files.len() + self.directories.len() >= MAX_FILES {
                return Err("actionlint workspace exceeds its file limit".into());
            }
            self.directories.insert(name);
        } else if path.is_file() {
            self.insert(name, vec![])?;
        }
        Ok(())
    }
    pub fn config(&self) -> Option<&'static str> {
        CONFIG
            .into_iter()
            .find(|name| self.files.contains_key(*name))
    }

    pub fn build(repo: &Path, inputs: &[WorkflowInput]) -> Result<Self, String> {
        let mut snapshot = Self {
            files: BTreeMap::new(),
            directories: BTreeSet::new(),
            bytes: 0,
        };
        if inputs.is_empty() || inputs.len() > 128 {
            return Err("preloaded workflow inputs must contain 1 through 128 files".into());
        }
        let mut paths = HashSet::new();
        let mut queue = VecDeque::new();
        for input in inputs {
            let relative = &input.relative_path;
            checked_relative(relative)?;
            let expected = repo.join(relative);
            if expected != input.path
                || absolute_path(&expected)
                    .map_err(|_| "workflow path cannot be resolved safely")?
                    != expected
            {
                return Err(
                    "preloaded workflow inputs do not belong to the provided repository".into(),
                );
            }
            if !expected
                .extension()
                .is_some_and(|s| s.eq_ignore_ascii_case("yaml") || s.eq_ignore_ascii_case("yml"))
            {
                return Err("preloaded workflow files must use a .yml or .yaml extension".into());
            }
            if !paths.insert(expected) {
                return Err("preloaded workflow inputs contain a duplicate path".into());
            }
            if input.content.len() > MAX_FILE {
                return Err("preloaded workflow input exceeds the hard 1048576-byte limit".into());
            }
            snapshot.insert(relative.to_owned(), input.content.clone())?;
            if snapshot.bytes > 8 * MAX_FILE {
                return Err(
                    "preloaded workflow inputs exceed the hard 8388608-byte total limit".into(),
                );
            }
            queue.push_back(relative.to_owned());
        }
        for path in CONFIG {
            snapshot.file(repo, path)?;
        }
        if CONFIG.iter().all(|p| snapshot.files.contains_key(*p)) {
            return Err("repository contains both supported native actionlint config files".into());
        }
        let mut processed = HashSet::new();
        let mut manifests = BTreeSet::new();
        let mut nodes = 0;
        let mut refs = 0;
        while let Some(path) = queue.pop_front() {
            if !processed.insert(path.clone()) {
                continue;
            }
            let content = snapshot
                .files
                .get_mut(&path)
                .expect("queued workflow has snapshot bytes");
            let Some(parsed) = parse_for_actionlint(content, &path)? else {
                continue;
            };
            count(
                &mut nodes,
                &mut refs,
                parsed.node_count,
                parsed.references.len(),
            )?;
            let prepared = prepare_workflow(Cow::Owned(std::mem::take(content)), &parsed)?;
            *content = prepared.content.into_owned();
            for (context, dep) in prepared.dependencies {
                if matches!(context, ReferenceContext::Job) {
                    if snapshot.file(repo, &dep)? {
                        queue.push_back(dep);
                    }
                } else {
                    for filename in ["action.yaml", "action.yml"] {
                        let name = if dep.is_empty() {
                            filename.to_owned()
                        } else {
                            format!("{dep}/{filename}")
                        };
                        snapshot.file(repo, &name)?;
                        if snapshot.files.contains_key(&name) {
                            manifests.insert(name);
                            break;
                        }
                    }
                }
            }
        }
        for name in manifests {
            let bytes = &snapshot.files[&name];
            if std::str::from_utf8(bytes).is_err() {
                continue;
            }
            let (n, deps) = parse_action_manifest(bytes, &name)?;
            count(&mut nodes, &mut refs, n, deps.len())?;
            for (field, value) in deps {
                if let Some(path) = runtime_path(&name, &field, &value)? {
                    snapshot.presence(repo, path)?;
                }
            }
        }
        Ok(snapshot)
    }

    pub fn archive(&self) -> Result<Vec<u8>, String> {
        self.build_archive(false)
    }

    /// Python's archive contract counts PAX path records and 10 KiB records.
    pub fn embedded_archive(&self) -> Result<Vec<u8>, String> {
        self.build_archive(true)
    }

    fn build_archive(&self, pax: bool) -> Result<Vec<u8>, String> {
        let mut dirs = self.directories.clone();
        dirs.extend([".git", ".github", ".github/workflows"].map(String::from));
        for path in self.files.keys().chain(self.directories.iter()) {
            for (i, _) in path.match_indices('/') {
                let parent = &path[..i];
                if self.files.contains_key(parent) {
                    return Err(
                        "actionlint workspace archive contains a file-directory collision".into(),
                    );
                }
                if !pax || self.files.contains_key(path) {
                    dirs.insert(parent.into());
                }
            }
            if dirs.len() + self.files.len() > MAX_ENTRIES {
                return Err("actionlint workspace archive exceeds its entry limit".into());
            }
        }
        if dirs.iter().any(|dir| self.files.contains_key(dir)) {
            return Err("actionlint workspace archive contains a file-directory collision".into());
        }
        let mut archive = tar::Builder::new(ArchiveBuffer(Vec::new()));
        let failure = |error: io::Error| {
            if !pax {
                "actionlint workspace archive cannot be created within its byte limit"
            } else if error.kind() == io::ErrorKind::FileTooLarge {
                "actionlint workspace archive exceeds its byte limit"
            } else {
                "actionlint workspace archive cannot be created"
            }
            .to_owned()
        };
        let items = dirs
            .iter()
            .map(|p| (p, None))
            .chain(self.files.iter().map(|(p, b)| (p, Some(b))));
        for (path, data) in items {
            let mut header = if pax {
                tar::Header::new_ustar()
            } else {
                tar::Header::new_gnu()
            };
            header.set_entry_type(if data.is_some() {
                tar::EntryType::Regular
            } else {
                tar::EntryType::Directory
            });
            header.set_mode(if data.is_some() { 0o444 } else { 0o555 });
            header.set_uid(0);
            header.set_gid(0);
            header.set_mtime(0);
            let content = data.map(Vec::as_slice).unwrap_or_default();
            header.set_size(content.len() as u64);
            if pax {
                let name = if data.is_none() {
                    Cow::Owned(format!("{path}/"))
                } else {
                    Cow::Borrowed(path.as_str())
                };
                let raw = name.as_bytes();
                let field = if raw.len() > 100 || !raw.is_ascii() {
                    archive
                        .append_pax_extensions([("path", raw)])
                        .map_err(failure)?;
                    b"PaxMember"
                } else {
                    raw
                };
                header.as_ustar_mut().expect("PAX member uses USTAR").name[..field.len()]
                    .copy_from_slice(field);
                header.set_cksum();
                archive.append(&header, content).map_err(failure)?;
            } else {
                archive
                    .append_data(&mut header, path, content)
                    .map_err(failure)?;
            }
        }
        let mut bytes = archive.into_inner().map_err(failure)?.0;
        if pax {
            let record_size = 10_240;
            let padded = bytes.len().next_multiple_of(record_size);
            if padded > MAX_ARCHIVE {
                return Err("actionlint workspace archive exceeds its byte limit".into());
            }
            bytes.resize(padded, 0);
        }
        Ok(bytes)
    }
}

fn count(nodes: &mut usize, refs: &mut usize, n: usize, r: usize) -> Result<(), String> {
    *nodes += n;
    *refs += r;
    if *nodes > 100_000 {
        return Err("actionlint workspace exceeds its YAML-node limit".into());
    }
    if *refs > 4096 {
        return Err("actionlint workspace exceeds its reference limit".into());
    }
    Ok(())
}

struct ArchiveBuffer(Vec<u8>);
impl Write for ArchiveBuffer {
    fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        if data.len() > MAX_ARCHIVE - self.0.len() {
            return Err(io::ErrorKind::FileTooLarge.into());
        }
        self.0.extend_from_slice(data);
        Ok(data.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
