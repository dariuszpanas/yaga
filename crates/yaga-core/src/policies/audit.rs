use super::{Error, Kind, pretty_json, validate};
use crate::{
    fs::{absolute_path, display_path, read_regular_prefix, resolve_user_path},
    text::{python_repr, safe_text},
};
use serde_json::json;
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};
use toml::Value;

const MAX_FILES: usize = 128;
const MAX_DIRS: usize = 256;
const MAX_BYTES: usize = 1024 * 1024;
fn display(p: &Path) -> String {
    safe_text(&display_path(p), 300)
}

/// Preserve the strict UTF-8 diagnostic reported by Python's policy reader.
/// Offsets refer to the input after removing its optional UTF-8 BOM.
fn decode_error(bytes: &[u8], error: std::str::Utf8Error) -> String {
    let start = error.valid_up_to();
    let first = bytes[start];
    let (length, reason) = match error.error_len() {
        None => (bytes.len() - start, "unexpected end of data"),
        Some(length) if (0xc2..=0xf4).contains(&first) => (length, "invalid continuation byte"),
        Some(length) => (length, "invalid start byte"),
    };
    let location = if length == 1 {
        format!("byte 0x{first:02x} in position {start}")
    } else {
        format!("bytes in position {start}-{}", start + length - 1)
    };
    format!("invalid UTF-8 TOML: 'utf-8' codec can't decode {location}: {reason}")
}

#[derive(Debug)]
pub struct AuditEntry {
    pub path: PathBuf,
    pub kind: Option<Kind>,
    pub version_key: Option<&'static str>,
    pub source_version: Option<i64>,
    pub current_version: Option<i64>,
    pub required_version: Option<String>,
    pub status: &'static str,
    pub message: String,
}
impl AuditEntry {
    fn invalid(path: &Path, kind: Option<Kind>, message: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            kind,
            version_key: None,
            source_version: None,
            current_version: None,
            required_version: None,
            status: "invalid",
            message: message.into(),
        }
    }
    fn name(&self) -> &'static str {
        self.kind.map_or("unknown", Kind::as_str)
    }
}
#[derive(Debug)]
pub struct AuditReport {
    pub entries: Vec<AuditEntry>,
    pub strict: bool,
}
#[derive(Clone, Copy, Debug)]
pub enum Format {
    Text,
    Json,
    Github,
}
impl AuditReport {
    fn count(&self, status: &str) -> usize {
        self.entries.iter().filter(|e| e.status == status).count()
    }
    pub fn exit_code(&self) -> u8 {
        if self.count("invalid") > 0 {
            2
        } else if self.strict && self.count("deprecated") > 0 {
            1
        } else {
            0
        }
    }
    pub fn render(&self, format: Format) -> String {
        let (current, deprecated, invalid) = (
            self.count("current"),
            self.count("deprecated"),
            self.count("invalid"),
        );
        let checked = self.entries.len();
        if matches!(format, Format::Json) {
            let status = match self.exit_code() {
                2 => "error",
                1 => "failed",
                _ => "passed",
            };
            let items:Vec<_>=self.entries.iter().map(|e|json!({"path":e.path.to_string_lossy(),"kind":e.name(),"version_key":e.version_key,
                "source_version":e.source_version,"current_version":e.current_version,"required_version":e.required_version,
                "status":e.status,"message":e.message})).collect();
            return pretty_json(
                &json!({"schema_version":1,"kind":"policy_check","status":status,"valid":self.exit_code()==0,
                "strict":self.strict,"checked":checked,"current":current,"deprecated":deprecated,"invalid":invalid,"policies":items}),
            );
        }
        let mut lines = vec![];
        for e in &self.entries {
            let name = e.name();
            let path = display(&e.path);
            if matches!(format, Format::Github) {
                if e.status == "current" {
                    continue;
                }
                let level = if e.status == "invalid" || self.strict {
                    "error"
                } else {
                    "warning"
                };
                let path = path
                    .replace('%', "%25")
                    .replace(':', "%3A")
                    .replace(',', "%2C");
                let message = safe_text(&e.message, 400).replace('%', "%25");
                lines.push(format!(
                    "::{level} title=YAGA policy,file={path}::[{name}] {message}"
                ));
            } else {
                let version = match (e.source_version, e.current_version) {
                    (Some(a), Some(b)) => format!("v{a}/v{b}"),
                    _ => "-".into(),
                };
                lines.push(format!(
                    "{:<10}  {name:<13}  {version:<7}  {path}  ({})",
                    e.status.to_uppercase(),
                    e.message
                ));
            }
        }
        lines.push(format!("Checked {checked} policy section(s): {current} current, {deprecated} deprecated, {invalid} invalid."));
        lines.join("\n")
    }
}

fn hint(path: &Path) -> Option<Kind> {
    let name = path.file_name()?.to_string_lossy().to_lowercase();
    if name == "pyproject.toml" {
        return Some(Kind::Commit);
    }
    if let Some(kind) = Kind::ALL.iter().find(|k| {
        k != &&Kind::Repo && Path::new(k.default_path()).file_name().unwrap() == name.as_str()
    }) {
        return Some(*kind);
    }
    if path
        .extension()
        .is_some_and(|s| s.eq_ignore_ascii_case("toml"))
        && path.components().any(|s| {
            s.as_os_str()
                .to_str()
                .is_some_and(|s| s.to_lowercase() == "checks")
        })
    {
        return Some(Kind::Repo);
    }
    None
}
/// Determine the policy families using only keys from the host's decoded document.
pub fn classify_policy<'a>(
    path: &Path,
    contains: impl Fn(&str) -> bool,
    pyproject_keys: impl Iterator<Item = &'a str>,
) -> Vec<Kind> {
    if path
        .file_name()
        .is_some_and(|name| name == "pyproject.toml")
    {
        let mut review = false;
        let mut other = false;
        for key in pyproject_keys {
            review |= key == "agent-review";
            other |= !matches!(key, "agent-review" | "required-version");
        }
        return match (review, other) {
            (true, true) => vec![Kind::Commit, Kind::AgentReview],
            (true, false) => vec![Kind::AgentReview],
            (false, _) => vec![Kind::Commit],
        };
    }
    for k in [
        Kind::Triage,
        Kind::Repo,
        Kind::Branch,
        Kind::Change,
        Kind::Description,
        Kind::Mode,
        Kind::Path,
        Kind::Size,
        Kind::Tree,
        Kind::Test,
    ] {
        if contains(k.version_key()) {
            return vec![k];
        }
    }
    let mut kinds = vec![];
    if contains("config-version") || contains("commit") {
        kinds.push(Kind::Commit);
    }
    if contains("agent-review") {
        kinds.push(Kind::AgentReview);
    }
    if kinds.is_empty()
        && let Some(k) = hint(path)
    {
        kinds.push(k);
    }
    kinds
}
fn audit_file(path: &Path) -> Vec<AuditEntry> {
    let resolved = match resolve_user_path(path) {
        Ok(p) => p,
        Err(error) => {
            return vec![AuditEntry::invalid(
                Path::new(&display_path(path)),
                None,
                format!(
                    "cannot resolve policy path: {}",
                    safe_text(&error.to_string(), 300)
                ),
            )];
        }
    };
    let path = resolved.as_path();
    let bad = |message: String| vec![AuditEntry::invalid(path, hint(path), message)];
    let raw = match read_regular_prefix(path, MAX_BYTES) {
        Ok(bytes) => bytes,
        Err(e) => {
            let message = if e.kind() == std::io::ErrorKind::NotFound {
                format!(
                    "[Errno 2] No such file or directory: {}",
                    python_repr(&path.to_string_lossy())
                )
            } else {
                e.to_string()
            };
            return bad(format!(
                "cannot read policy file: {}",
                safe_text(&message, 300)
            ));
        }
    };
    if raw.len() > MAX_BYTES {
        return bad(format!("policy file exceeds {MAX_BYTES} bytes"));
    }
    let bytes = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&raw);
    let text = match std::str::from_utf8(bytes) {
        Ok(s) => s,
        Err(e) => return bad(decode_error(bytes, e)),
    };
    let doc: toml::Table = match toml::from_str(text) {
        Ok(t) => t,
        Err(e) => {
            return bad(format!(
                "invalid UTF-8 TOML: {}",
                safe_text(e.message(), 300)
            ));
        }
    };
    let yaga_keys = doc
        .get("tool")
        .and_then(|t| t.get("yaga"))
        .and_then(Value::as_table)
        .into_iter()
        .flat_map(|t| t.keys().map(String::as_str));
    let kinds = classify_policy(path, |k| doc.contains_key(k), yaga_keys);
    if kinds.is_empty() {
        return vec![AuditEntry::invalid(
            path,
            None,
            "unrecognized YAGA policy file; expected a known *-version key or [agent-review] table",
        )];
    }
    let root = if path.ends_with("pyproject.toml") {
        doc.get("tool")
            .and_then(|t| t.get("yaga"))
            .and_then(Value::as_table)
    } else {
        Some(&doc)
    };
    kinds
        .into_iter()
        .map(|kind| {
            let section = if kind == Kind::AgentReview {
                root.and_then(|t| t.get("agent-review"))
                    .and_then(Value::as_table)
            } else {
                root
            };
            let key = kind.version_key();
            let v = section.and_then(|t| t.get(key));
            let mut source = v
                .and_then(Value::as_integer)
                .or_else(|| (kind == Kind::Commit && v.is_none()).then_some(1));
            let required = section
                .and_then(|t| t.get("required-version"))
                .or_else(|| root.and_then(|t| t.get("required-version")))
                .and_then(Value::as_str);
            let (status, message) = match validate(kind, &raw, path) {
                Ok(actual) => {
                    source = Some(actual);
                    if actual < kind.current() {
                        (
                            "deprecated",
                            format!(
                                "{key} = {actual} is deprecated; latest schema version is {}",
                                kind.current()
                            ),
                        )
                    } else {
                        ("current", format!("{key} = {actual} is current"))
                    }
                }
                Err(e) => ("invalid", safe_text(&e, 400)),
            };
            AuditEntry {
                path: path.into(),
                kind: Some(kind),
                version_key: Some(key),
                source_version: source,
                current_version: Some(kind.current()),
                required_version: required.map(str::to_owned),
                status,
                message,
            }
        })
        .collect()
}

fn inside(path: &Path, repo: &Path) -> Result<PathBuf, Error> {
    absolute_path(path)
        .ok()
        .filter(|p| p.starts_with(repo))
        .ok_or_else(|| {
            Error::Configuration(format!(
                "policy path escapes repository {}: {}",
                display(repo),
                display(path)
            ))
        })
}
fn audit_pyproject(path: &Path) -> bool {
    let Ok(raw) = read_regular_prefix(path, MAX_BYTES) else {
        return true;
    };
    if raw.len() > MAX_BYTES {
        return true;
    }
    let Ok(s) = std::str::from_utf8(raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&raw)) else {
        return true;
    };
    let Ok(doc) = toml::from_str::<toml::Table>(s) else {
        return true;
    };
    doc.get("tool")
        .is_some_and(|v| v.as_table().is_none_or(|t| t.contains_key("yaga")))
}
fn discover(repo: &Path) -> Result<Vec<PathBuf>, Error> {
    let mut files = Vec::new();
    let mut seen = HashSet::new();
    for name in [".yaga.toml", "pyproject.toml"] {
        let path = repo.join(name);
        if path.exists() || path.is_symlink() {
            let p = inside(&path, repo)?;
            if (name != "pyproject.toml" || audit_pyproject(&p)) && seen.insert(p.clone()) {
                files.push(p);
            }
        }
    }
    let mut found = vec![];
    let root = inside(&repo.join(".yaga"), repo)?;
    if root.is_dir() {
        let mut visited = HashSet::from([root.clone()]);
        let mut todo = vec![root];
        while let Some(dir) = todo.pop() {
            let failure =
                |_| Error::Configuration(format!("cannot scan policy directory {}", display(&dir)));
            for child in fs::read_dir(&dir).map_err(failure)? {
                let path = child.map_err(failure)?.path();
                let p = inside(&path, repo)?;
                if p.is_dir() {
                    if visited.insert(p.clone()) {
                        if visited.len() > MAX_DIRS {
                            return Err(Error::Configuration(format!(
                                "discovered policy directory count exceeds {MAX_DIRS} directories in {}",
                                display(repo)
                            )));
                        }
                        todo.push(p);
                    }
                } else if path
                    .extension()
                    .is_some_and(|s| s.eq_ignore_ascii_case("toml"))
                    && seen.insert(p.clone())
                {
                    if seen.len() > MAX_FILES {
                        return Err(Error::Configuration(format!(
                            "discovered policy count exceeds {MAX_FILES} files in {}",
                            display(repo)
                        )));
                    }
                    found.push(p);
                }
            }
        }
    }
    found.sort_by_cached_key(|path| {
        path.iter()
            .map(|c| c.to_string_lossy())
            .collect::<Vec<_>>()
            .join("/")
    });
    files.extend(found);
    Ok(files)
}

pub fn audit_policies(repo: &Path, paths: &[PathBuf], strict: bool) -> Result<AuditReport, Error> {
    let selected = if paths.is_empty() {
        let repo = resolve_user_path(repo)
            .map_err(|_| Error::Input(format!("cannot resolve repository {}", display(repo))))?;
        if !repo.is_dir() {
            return Err(Error::Input(format!(
                "repository is not a directory: {}",
                display(&repo)
            )));
        }
        let files = discover(&repo)?;
        if files.is_empty() {
            return Err(Error::Input(format!(
                "no YAGA policy files found in {}; run 'yaga policy init' or pass explicit file paths",
                display(&repo)
            )));
        }
        files
    } else {
        if paths.len() > MAX_FILES {
            return Err(Error::Input(format!(
                "cannot audit more than {MAX_FILES} policy paths at once"
            )));
        }
        let mut seen = HashSet::new();
        paths
            .iter()
            .filter(|p| seen.insert(resolve_user_path(p).unwrap_or_else(|_| p.to_path_buf())))
            .cloned()
            .collect()
    };
    Ok(AuditReport {
        entries: selected.iter().flat_map(|p| audit_file(p)).collect(),
        strict,
    })
}
