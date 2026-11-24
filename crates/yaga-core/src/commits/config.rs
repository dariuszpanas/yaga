use serde::Serialize;
use serde::de::DeserializeOwned;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use toml::{Table, Value};

use super::{policy::*, validate};
use crate::fs::resolve_user_path;
use crate::version::{REQUIREMENT_TYPE_ERROR, check_requirement};

pub const MAX_CONFIG_BYTES: usize = 1024 * 1024;

#[derive(Debug, Clone, Serialize)]
pub struct QualityPolicy {
    pub provider: QualityProvider,
    pub task: QualityTask,
    #[serde(rename = "model_id")]
    pub model: String,
    pub revision: Option<String>,
    pub threshold: f64,
    pub region: Option<String>,
    pub max_tokens: usize,
    pub max_input_tokens: usize,
    pub input_mode: QualityInputMode,
}

impl Default for QualityPolicy {
    fn default() -> Self {
        Self {
            provider: QualityProvider::Huggingface,
            task: QualityTask::Classification,
            model: "saridormi/commit-message-quality-codebert".into(),
            revision: Some("30c7895b3eb0270a3246ef3db7b43c837d8e553a".into()),
            threshold: 0.70,
            region: None,
            max_tokens: 32,
            max_input_tokens: 512,
            input_mode: QualityInputMode::Message,
        }
    }
}

impl QualityPolicy {
    /// Reset the provider-specific defaults when an explicit choice overrides configuration.
    pub fn select_provider(&mut self, provider: QualityProvider) {
        if provider == self.provider {
            return;
        }
        self.provider = provider;
        self.task = QualityTask::Classification;
        self.region = None;
        self.revision = None;
        self.model = match provider {
            QualityProvider::Huggingface => {
                let legacy = Self::default();
                self.revision = legacy.revision;
                legacy.model
            }
            QualityProvider::Bedrock => "amazon.nova-micro-v1:0".into(),
            _ => String::new(),
        };
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct LoadedConfig {
    #[serde(flatten)]
    pub policy: CommitPolicy,
    #[serde(skip)]
    pub path: Option<PathBuf>,
    pub required_version: Option<String>,
    pub max_commits: usize,
    pub typos: TyposPolicy,
    pub typos_config: TyposConfig,
    pub pull_request_message: PullRequestMessage,
    pub skip_pull_request_authors: Vec<String>,
    pub dependabot_pull_requests: DependabotPullRequests,
    pub dependabot_require_github_signature: bool,
    pub quality: QualityPolicy,
}

impl Default for LoadedConfig {
    fn default() -> Self {
        Self {
            policy: CommitPolicy::default(),
            path: None,
            required_version: None,
            max_commits: 256,
            typos: TyposPolicy::Skip,
            typos_config: TyposConfig::Repository,
            pull_request_message: PullRequestMessage::TitleOnly,
            skip_pull_request_authors: Vec::new(),
            dependabot_pull_requests: DependabotPullRequests::Check,
            dependabot_require_github_signature: true,
            quality: QualityPolicy::default(),
        }
    }
}

fn display(path: &Path) -> String {
    crate::text::safe_text(&path.to_string_lossy(), 300)
}

fn document(bytes: &[u8], path: &Path) -> Result<Table, String> {
    if bytes.len() > MAX_CONFIG_BYTES {
        return Err(format!(
            "configuration exceeds {MAX_CONFIG_BYTES} bytes: {}",
            display(path)
        ));
    }
    let text = std::str::from_utf8(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes))
        .map_err(|_| format!("configuration is not valid UTF-8: {}", display(path)))?;
    toml::from_str(text).map_err(|e| {
        format!(
            "invalid TOML in {}: {}",
            display(path),
            crate::text::safe_text(e.message(), 300)
        )
    })
}

fn root(mut document: Table, path: &Path) -> Result<Option<Table>, String> {
    if path.file_name().is_none_or(|s| s != "pyproject.toml") {
        return Ok(Some(document));
    }
    let Some(tool) = document.remove("tool") else {
        return Ok(None);
    };
    let Value::Table(mut tool) = tool else {
        return Err(format!("[tool] must be a table in {}", display(path)));
    };
    tool.remove("yaga")
        .map(|v| {
            if let Value::Table(t) = v {
                Ok(t)
            } else {
                Err(format!(
                    "YAGA configuration must be a table in {}",
                    display(path)
                ))
            }
        })
        .transpose()
}

pub fn parse_config(bytes: &[u8], path: &Path) -> Result<LoadedConfig, String> {
    let root = root(document(bytes, path)?, path)?
        .ok_or_else(|| format!("{} has no [tool.yaga] table", display(path)))?;
    parse_root(root, path)
}

/// Select a single explicit or nearest policy, stopping at the first .git
/// boundary. Supply a global path only when the installation permits fallback.
pub fn load_config(
    explicit: Option<&Path>,
    start: &Path,
    global: Option<&Path>,
) -> Result<LoadedConfig, String> {
    if let Some(path) = explicit {
        let path = resolve_user_path(path).map_err(|_| {
            format!(
                "cannot resolve configuration {}",
                crate::text::safe_text(&crate::fs::display_path(path), 300)
            )
        })?;
        return parse_config(&read(&path)?, &path);
    }
    let start = resolve_user_path(start).map_err(|_| {
        format!(
            "cannot resolve configuration start directory {}",
            crate::text::safe_text(&crate::fs::display_path(start), 300)
        )
    })?;
    if !start.is_dir() {
        return Err(format!(
            "configuration start directory does not exist: {}",
            display(&start)
        ));
    }
    for dir in start.ancestors() {
        let standalone = dir.join(".yaga.toml");
        if standalone.is_file() {
            return parse_config(&read(&standalone)?, &standalone);
        }
        let pyproject = dir.join("pyproject.toml");
        if pyproject.is_file()
            && let Some(t) = root(document(&read(&pyproject)?, &pyproject)?, &pyproject)?
        {
            return parse_root(t, &pyproject);
        }
        if dir.join(".git").exists() {
            break;
        }
    }
    match global.filter(|p| p.is_file()) {
        Some(path) => load_config(Some(path), &start, None),
        None => Ok(LoadedConfig::default()),
    }
}

/// Standalone installations follow the user configuration directory outside
/// CI. Python adapters own their existing virtual environment eligibility.
pub fn global_config_path() -> Option<PathBuf> {
    if [
        "CI",
        "GITHUB_ACTIONS",
        "YAGA_ACTION_RUNTIME",
        "YAGA_COMMIT_ACTION_RUNTIME",
    ]
    .iter()
    .any(|k| std::env::var_os(k).is_some_and(|v| !v.is_empty()))
    {
        return None;
    }
    #[cfg(windows)]
    let base = std::env::var_os("APPDATA")
        .map(PathBuf::from)
        .or_else(|| std::env::home_dir().map(|d| d.join("AppData/Roaming")));
    #[cfg(not(windows))]
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::home_dir().map(|d| d.join(".config")));
    base.filter(|p| p.is_absolute())
        .map(|p| p.join("yaga/config.toml"))
}

fn read(path: &Path) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| {
            file.take(MAX_CONFIG_BYTES as u64 + 1)
                .read_to_end(&mut bytes)
        })
        .map_err(|_| format!("cannot read configuration {}", display(path)))?;
    Ok(bytes)
}

fn take<T: DeserializeOwned>(table: &mut Table, key: &str, default: T) -> Result<T, String> {
    let Some(v) = table.remove(key) else {
        return Ok(default);
    };
    v.try_into().map_err(|_| format!("invalid value for {key}"))
}

/// Resolve and validate an already-decoded standalone or tool.yaga table.
pub fn parse_root(root: Table, path: &Path) -> Result<LoadedConfig, String> {
    parse_root_table(root, path)
}

fn parse_root_table(mut root: Table, path: &Path) -> Result<LoadedConfig, String> {
    let required_version = root
        .remove("required-version")
        .map(|v| {
            let text = v
                .as_str()
                .ok_or_else(|| String::from(REQUIREMENT_TYPE_ERROR))?;
            check_requirement(text)?;
            Ok::<_, String>(text.to_owned())
        })
        .transpose()?;
    let where_ = display(path);
    let mut unknown: Vec<_> = root
        .keys()
        .filter(|key| !matches!(key.as_str(), "config-version" | "agent-review" | "commit"))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        unknown.sort_unstable();
        return Err(format!(
            "unknown YAGA configuration key(s) in {where_}: {}",
            crate::text::safe_text(&unknown.join(", "), 1000)
        ));
    }
    if let Some(v) = root.remove("agent-review")
        && !v.is_table()
    {
        return Err(format!("agent-review must be a table in {where_}"));
    }
    if let Some(v) = root.remove("config-version")
        && v.as_integer() != Some(1)
    {
        let range = format!("config-version must be an integer from 1 through 1 in {where_}");
        return Err(match v.as_integer() {
            Some(version) if version > 1 => format!(
                "{range} (found newer schema version {version}; update YAGA using its installation manager)"
            ),
            _ => range,
        });
    }
    let commit = root.remove("commit").unwrap_or(Value::Table(Table::new()));
    let Value::Table(mut commit) = commit else {
        return Err(format!("commit must be a table in {where_}"));
    };
    let mut config = parse_commit(&mut commit).map_err(|e| commit_error(e, &commit, &where_))?;
    config.path = Some(path.into());
    config.required_version = required_version;
    Ok(config)
}

// The decoder consumes recognized adapter options and retains the original spellings
// of policy keys. Consult the resolved schema only when a rejected input needs a
// field diagnostic, including all unknown keys before individual value errors.
#[cold]
fn commit_error(error: CommitConfigError, table: &Table, path: &str) -> String {
    let fields = serde_json::to_value(LoadedConfig::default()).expect("serializable defaults");
    let mut unknown: Vec<_> = table
        .keys()
        .filter(|key| {
            key.contains('_')
                || key.as_str() == "required-version"
                || fields.get(key.replace('-', "_")).is_none()
        })
        .map(String::as_str)
        .collect();
    if unknown.is_empty() {
        let (message, _, unknown_quality) = error.details();
        return if unknown_quality.is_empty() {
            format!("{message} in {path}")
        } else {
            format!(
                "{message} in {path}: {}",
                crate::text::safe_text(&unknown_quality.join(", "), 1000)
            )
        };
    }
    unknown.sort_unstable();
    format!(
        "unknown commit configuration key(s) in {path}: {}",
        crate::text::safe_text(&unknown.join(", "), 1000)
    )
}

fn parse_commit(table: &mut Table) -> Result<LoadedConfig, CommitConfigError> {
    // TOML's serde decoder can coerce a datetime into a string. No commit
    // option permits dates, including inside arrays or provider tables.
    let quality = table.remove("quality");
    if crate::policies::has_datetime(table) {
        return Err("commit configuration does not allow TOML datetime values".into());
    }
    let mut config = LoadedConfig::default();
    config.typos = take(table, "typos", config.typos)?;
    config.typos_config = take(table, "typos-config", config.typos_config)?;
    config.max_commits = take(table, "max-commits", config.max_commits)?;
    config.dependabot_pull_requests = take(
        table,
        "dependabot-pull-requests",
        config.dependabot_pull_requests,
    )?;
    config.dependabot_require_github_signature =
        take(table, "dependabot-require-github-signature", true)?;
    config.pull_request_message = take(table, "pull-request-message", config.pull_request_message)?;
    config.skip_pull_request_authors = take(table, "skip-pull-request-authors", Vec::new())?;
    if let Some(quality) = quality {
        let Value::Table(options) = quality else {
            return Err("quality must be a table".into());
        };
        config.quality = QualityPolicy::from_options(&options)?;
    }

    let conventional_only = [
        "allowed-types",
        "type-case",
        "scope-policy",
        "allowed-scopes",
        "scope-case",
        "scope-policy-by-type",
        "body-policy-by-type",
        "breaking-markers",
    ]
    .iter()
    .any(|k| table.contains_key(*k));
    // The evaluator consumes resolved mappings as pairs. The wire format is
    // a table and accepts only kebab-case keys.
    let mut resolved = Table::new();
    for (key, raw) in table {
        let mut value = std::mem::replace(raw, Value::Boolean(false));
        if key.contains('_') {
            return Err(format!(
                "unknown commit configuration key {}",
                crate::text::safe_text(key, 128)
            )
            .into());
        }
        if matches!(
            key.as_str(),
            "scope-policy-by-type" | "body-policy-by-type" | "footer-values"
        ) {
            let Value::Table(map) = value else {
                return Err(format!("{key} must be a table").into());
            };
            value = Value::Array(
                map.into_iter()
                    .map(|(k, v)| Value::Array(vec![Value::String(k), v]))
                    .collect(),
            );
        }
        resolved.insert(key.replace('-', "_"), value);
    }
    config.policy = Value::Table(resolved).try_into().map_err(|e| {
        format!(
            "invalid commit configuration: {}",
            crate::text::safe_text(e.message(), 300)
        )
    })?;
    validate::policy(&config, conventional_only)?;
    Ok(config)
}

/// Field and key information for hosts that preserve their native error context.
#[derive(Debug)]
pub enum CommitConfigError {
    Field(String),
    Choice(&'static str, &'static str),
    UnknownKeys(Vec<String>),
}

impl CommitConfigError {
    pub fn details(self) -> (String, Option<&'static str>, Vec<String>) {
        match self {
            Self::Field(message) => (message, None, Vec::new()),
            Self::Choice(key, choices) => (
                format!("quality.{key} must be one of {choices}"),
                Some(key),
                Vec::new(),
            ),
            Self::UnknownKeys(keys) => (
                "unknown commit.quality configuration key(s)".into(),
                None,
                keys,
            ),
        }
    }
}

impl From<String> for CommitConfigError {
    fn from(message: String) -> Self {
        Self::Field(message)
    }
}

impl From<&str> for CommitConfigError {
    fn from(message: &str) -> Self {
        Self::Field(message.to_owned())
    }
}

fn quality_choice<T: DeserializeOwned>(
    table: &Table,
    key: &'static str,
    default: T,
    choices: &'static str,
) -> Result<T, CommitConfigError> {
    let Some(value) = table.get(key) else {
        return Ok(default);
    };
    if !value.is_str() {
        return Err(CommitConfigError::Field(format!(
            "quality.{key} must be a string"
        )));
    }
    value
        .clone()
        .try_into()
        .map_err(|_| CommitConfigError::Choice(key, choices))
}

fn quality_integer(
    table: &Table,
    key: &str,
    default: usize,
    max: usize,
) -> Result<usize, CommitConfigError> {
    let Some(value) = table.get(key) else {
        return Ok(default);
    };
    value
        .as_integer()
        .and_then(|n| usize::try_from(n).ok())
        .filter(|n| (1..=max).contains(n))
        .ok_or_else(|| {
            CommitConfigError::Field(format!(
                "quality.{key} must be an integer from 1 through {max}"
            ))
        })
}

impl QualityPolicy {
    /// Resolve the optional advisory configuration in schema-v1 diagnostic order.
    pub fn from_options(table: &Table) -> Result<Self, CommitConfigError> {
        const KEYS: &[&str] = &[
            "provider",
            "task",
            "input-mode",
            "model",
            "revision",
            "threshold",
            "region",
            "max-tokens",
            "max-input-tokens",
        ];
        let mut unknown: Vec<_> = table
            .keys()
            .filter(|key| !KEYS.contains(&key.as_str()))
            .cloned()
            .collect();
        if !unknown.is_empty() {
            unknown.sort_unstable();
            return Err(CommitConfigError::UnknownKeys(unknown));
        }
        let mut q = Self::default();
        q.select_provider(quality_choice(
            table,
            "provider",
            q.provider,
            "huggingface, bedrock, openai, anthropic, gemini, openai-compatible",
        )?);
        q.task = quality_choice(table, "task", q.task, "classification, seq2seq")?;
        q.input_mode = quality_choice(table, "input-mode", q.input_mode, "message, title")?;
        if q.provider != QualityProvider::Huggingface && q.task == QualityTask::Seq2seq {
            return Err(
                "quality.task = 'seq2seq' is supported only by the Hugging Face provider".into(),
            );
        }
        let ascii = |text: &str, max| {
            (1..=max).contains(&text.len()) && text.bytes().all(|b| (0x21..=0x7e).contains(&b))
        };
        const MODEL: &str = "quality.model must be printable ASCII of 1-256 characters";
        if let Some(v) = table.get("model") {
            q.model = v.as_str().ok_or(MODEL)?.to_owned();
        }
        if q.provider.requires_explicit_model() && q.model.is_empty() {
            return Err(EXPLICIT_QUALITY_MODEL.trim_end_matches('.').into());
        }
        if !ascii(&q.model, 256) {
            return Err(MODEL.into());
        }
        if let Some(value) = table.get("revision") {
            if q.provider != QualityProvider::Huggingface {
                return Err(
                    "quality.revision is supported only by the Hugging Face provider".into(),
                );
            }
            let revision = value
                .as_str()
                .filter(|s| {
                    s.len() == 40
                        && s.bytes()
                            .all(|b| b.is_ascii_digit() || matches!(b, b'a'..=b'f'))
                })
                .ok_or("quality.revision must be a 40-character lowercase hexadecimal SHA")?;
            q.revision = Some(revision.to_owned());
        }
        if let Some(value) = table.get("threshold") {
            q.threshold = value
                .as_float()
                .or_else(|| value.as_integer().map(|v| v as f64))
                .filter(|v| (0.0..=1.0).contains(v))
                .ok_or("quality.threshold must be at least 0 and at most 1")?;
        }
        if let Some(value) = table.get("region") {
            if q.provider.requires_explicit_model() {
                return Err("quality.region is supported only by Bedrock".into());
            }
            let region = value
                .as_str()
                .filter(|s| ascii(s, 64))
                .ok_or("quality.region must be printable ASCII of 1-64 characters")?;
            q.region = Some(region.to_owned());
        }
        q.max_tokens = quality_integer(table, "max-tokens", q.max_tokens, 256)?;
        q.max_input_tokens = quality_integer(table, "max-input-tokens", q.max_input_tokens, 4096)?;
        Ok(q)
    }
}
