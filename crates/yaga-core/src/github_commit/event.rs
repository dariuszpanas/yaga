use crate::commit_source::validate_title;
use crate::commits::MAX_MESSAGE_BYTES;
use crate::fs::{read_regular_prefix, resolve_user_path};
use crate::json::strict_value;
use regex::Regex;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

pub const MAX_EVENT_BYTES: usize = 1024 * 1024;

/// Only the bounded fields consumed by commit policy, parsed independently of Git.
#[derive(Debug, serde::Serialize)]
pub struct PullRequestEvent {
    pub action: String,
    pub number: i64,
    pub title: String,
    pub body: String,
    pub base_sha: String,
    pub head_sha: String,
    pub base_ref: String,
    pub head_ref: String,
    pub repository: String,
    pub repository_id: i64,
    pub draft: bool,
    pub author_login: String,
    pub author_id: i64,
    pub author_type: String,
    pub head_repository: String,
    pub default_branch: Option<String>,
}

/// Scalar kinds needed by the pull-request payload. Unused children are never visited.
#[doc(hidden)]
pub enum EventScalar<'a> {
    Missing,
    Object,
    Boolean(bool),
    Integer(i64),
    Text(&'a str),
    Other,
}

/// Read fields from an already decoded event. The host owns Unicode validation
/// for titles and references, and preserves its native string encoding errors.
#[doc(hidden)]
pub trait EventValue: Sized {
    type Error: From<String> + From<&'static str>;

    fn field(&self, key: &str) -> Result<Self, Self::Error>;
    fn scalar(&self) -> EventScalar<'_>;
    fn checked_text(&self, label: &'static str) -> Result<String, Self::Error>;
}

impl EventValue for Option<&Value> {
    type Error = String;

    fn field(&self, key: &str) -> Result<Self, String> {
        Ok(self.and_then(|v| v.get(key)))
    }

    fn scalar(&self) -> EventScalar<'_> {
        match self {
            None => EventScalar::Missing,
            Some(Value::Object(_)) => EventScalar::Object,
            Some(Value::Bool(v)) => EventScalar::Boolean(*v),
            Some(Value::Number(v)) => v.as_i64().map_or(EventScalar::Other, EventScalar::Integer),
            Some(Value::String(v)) => EventScalar::Text(v),
            _ => EventScalar::Other,
        }
    }

    fn checked_text(&self, label: &'static str) -> Result<String, String> {
        let value = self.unwrap_or(&Value::Null);
        match label {
            "pull request author login" => Ok(value.as_str().unwrap_or_default().into()),
            "pull request title" => {
                let text = value.as_str().unwrap_or_default();
                validate_title(text, label)?;
                Ok(text.into())
            }
            "pull request body" => match value {
                Value::Null => Ok(String::new()),
                Value::String(text) => Ok(text.clone()),
                _ => Err("pull request body must be a string or null".into()),
            },
            _ => reference(value, label),
        }
    }
}

fn object<V: EventValue>(v: V, label: &str) -> Result<V, V::Error> {
    if matches!(v.scalar(), EventScalar::Object) {
        Ok(v)
    } else {
        Err(format!("{label} must be an object").into())
    }
}
fn positive<V: EventValue>(v: V, label: &str) -> Result<i64, V::Error> {
    match v.scalar() {
        EventScalar::Integer(n) if n > 0 => Ok(n),
        _ => Err(format!("{label} must be a bounded positive integer").into()),
    }
}
fn name<V: EventValue>(v: V, label: &str) -> Result<String, V::Error> {
    static NAME: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\z").unwrap());
    if let EventScalar::Text(s) = v.scalar()
        && NAME.is_match(s)
    {
        if s.split('/').any(|p| matches!(p, "." | "..")) {
            return Err(format!("{label} contains an unsafe path segment").into());
        }
        return Ok(s.into());
    }
    Err(format!("{label} must be an owner/name pair").into())
}
pub(super) fn valid_oid(s: &str) -> bool {
    matches!(s.len(), 40 | 64)
        && s.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
fn oid<V: EventValue>(v: V, label: &str) -> Result<String, V::Error> {
    match v.scalar() {
        EventScalar::Text(s) if valid_oid(s) => Ok(s.into()),
        _ => Err(format!("{label} must be a full lowercase object ID").into()),
    }
}
fn reference(v: &Value, label: &str) -> Result<String, String> {
    static UNSAFE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]").unwrap());
    let s = v
        .as_str()
        .filter(|s| {
            !s.is_empty()
                && s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                    == *s
        })
        .ok_or_else(|| format!("{label} must be a bounded ref name"))?;
    if s.len() > 255 || UNSAFE.is_match(s) {
        return Err(format!("{label} contains unsafe or oversized text"));
    }
    Ok(s.into())
}
pub fn load_event(path: &Path) -> Result<PullRequestEvent, String> {
    read_event(resolve_user_path(path))
}

pub(super) fn read_event(path: std::io::Result<PathBuf>) -> Result<PullRequestEvent, String> {
    let raw = path
        .and_then(|p| read_regular_prefix(&p, MAX_EVENT_BYTES))
        .map_err(|_| "pull request event could not be read")?;
    parse_event(&raw)
}
pub(super) fn validate_proposed_message_size(
    title: &str,
    body: &str,
    maximum: usize,
) -> Result<(), String> {
    if title.len() + 2 + body.len() > maximum.min(MAX_MESSAGE_BYTES) {
        return Err("proposed pull request message exceeds the hard message byte limit".into());
    }
    Ok(())
}

pub fn parse_event(raw: &[u8]) -> Result<PullRequestEvent, String> {
    parse_event_with_message_limit(raw, MAX_MESSAGE_BYTES)
}

/// Parse one bounded event with a possibly tighter limit for its proposed commit message.
pub fn parse_event_with_message_limit(
    raw: &[u8],
    max_message_bytes: usize,
) -> Result<PullRequestEvent, String> {
    if raw.is_empty() {
        return Err("pull request event is empty".into());
    }
    if raw.len() > MAX_EVENT_BYTES {
        return Err(format!(
            "pull request event exceeds the hard {MAX_EVENT_BYTES}-byte limit"
        ));
    }
    std::str::from_utf8(raw).map_err(|_| "pull request event is not valid UTF-8")?;
    let doc = strict_value(raw).map_err(|_| "pull request event is invalid or ambiguous JSON")?;
    parse_event_fields(Some(&doc), max_message_bytes)
}

/// Validate fields from a trusted decoder of a bounded event, in policy order.
/// Rust consumers of raw or file-backed events should use `parse_event` or `load_event`.
#[doc(hidden)]
pub fn parse_event_fields<V: EventValue>(
    document: V,
    max_message_bytes: usize,
) -> Result<PullRequestEvent, V::Error> {
    let root = object(document, "pull request event")?;
    let pr = object(root.field("pull_request")?, "pull request")?;
    let user = object(pr.field("user")?, "pull request author")?;
    static LOGIN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\A[A-Za-z0-9_.-]+(?:\[bot\])?\z").unwrap());
    static TYPE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\A[A-Za-z][A-Za-z0-9_]{0,31}\z").unwrap());
    let author_login = user
        .field("login")?
        .checked_text("pull request author login")?;
    if author_login.len() > 128 || !LOGIN.is_match(&author_login) {
        return Err("pull request author login must be a bounded GitHub login".into());
    }
    let author_id = positive(user.field("id")?, "pull request author ID")?;
    let user_type = user.field("type")?;
    let author_type = match user_type.scalar() {
        EventScalar::Text(s) if TYPE.is_match(s) => s.to_owned(),
        _ => return Err("pull request author type must be a bounded GitHub account type".into()),
    };
    let repo = object(root.field("repository")?, "event repository")?;
    let repository_id = positive(repo.field("id")?, "event repository ID")?;
    let repository = name(repo.field("full_name")?, "event repository")?;
    let number = positive(root.field("number")?, "pull request number")?;
    if number != positive(pr.field("number")?, "pull request object number")? {
        return Err("pull request numbers do not match".into());
    }
    let action_field = root.field("action")?;
    let action = match action_field.scalar() {
        EventScalar::Text(
            s @ ("edited" | "opened" | "ready_for_review" | "reopened" | "synchronize"),
        ) => s,
        _ => return Err("pull request action is invalid".into()),
    };
    if !matches!(pr.field("state")?.scalar(), EventScalar::Text("open")) {
        return Err("pull request state must be open".into());
    }
    let EventScalar::Boolean(draft) = pr.field("draft")?.scalar() else {
        return Err("pull request draft state must be a boolean".into());
    };
    if action == "ready_for_review" && draft {
        return Err("ready_for_review event cannot describe a draft pull request".into());
    }
    let title = pr.field("title")?.checked_text("pull request title")?;
    let body = pr.field("body")?.checked_text("pull request body")?;
    validate_proposed_message_size(&title, &body, max_message_bytes)?;
    let base = object(pr.field("base")?, "pull request base")?;
    let head = object(pr.field("head")?, "pull request head")?;
    let base_repo = object(base.field("repo")?, "pull request base repository")?;
    let bid = positive(base_repo.field("id")?, "pull request base repository ID")?;
    let bname = name(
        base_repo.field("full_name")?,
        "pull request base repository",
    )?;
    if bid != repository_id || bname != repository {
        return Err("pull request base repository does not match the event repository".into());
    }
    let head_repo = object(head.field("repo")?, "pull request head repository")?;
    let hid = positive(head_repo.field("id")?, "pull request head repository ID")?;
    let head_repository = name(
        head_repo.field("full_name")?,
        "pull request head repository",
    )?;
    if (hid == repository_id) != (head_repository == repository) {
        return Err("pull request head repository identity is inconsistent".into());
    }
    let base_sha = oid(base.field("sha")?, "pull request base SHA")?;
    let head_sha = oid(head.field("sha")?, "pull request head SHA")?;
    let base_ref = base.field("ref")?.checked_text("pull request base ref")?;
    let head_ref = head.field("ref")?.checked_text("pull request head ref")?;
    if base_sha.len() != head_sha.len() {
        return Err("pull request base and head use different object ID formats".into());
    }
    if base_sha == head_sha {
        return Err("pull request base and head must differ".into());
    }
    let branch = repo.field("default_branch")?;
    let default_branch = if matches!(branch.scalar(), EventScalar::Missing) {
        None
    } else {
        Some(branch.checked_text("repository default branch")?)
    };
    Ok(PullRequestEvent {
        action: action.into(),
        number,
        title,
        body,
        base_sha,
        head_sha,
        base_ref,
        head_ref,
        repository,
        repository_id,
        draft,
        author_login,
        author_id,
        author_type,
        head_repository,
        default_branch,
    })
}

/// Independently supplied runner context for tokenless Action consumers.
pub struct RunnerContext<'a> {
    pub event_name: &'a str,
    pub repository: &'a str,
    pub repository_id: i64,
    pub base_ref: &'a str,
    pub head_ref: &'a str,
}
impl RunnerContext<'_> {
    pub fn validate(&self, event: &PullRequestEvent, trusted_policy: bool) -> Result<(), String> {
        let required = if trusted_policy {
            "pull_request_target"
        } else {
            "pull_request"
        };
        if self.event_name != required {
            return Err(format!("GitHub event name must be {required}"));
        }
        for (ok, label) in [
            (self.repository == event.repository, "repository"),
            (self.repository_id == event.repository_id, "repository ID"),
            (self.base_ref == event.base_ref, "base ref"),
            (self.head_ref == event.head_ref, "head ref"),
        ] {
            if !ok {
                return Err(format!("GitHub {label} does not match the event payload"));
            }
        }
        Ok(())
    }
}
