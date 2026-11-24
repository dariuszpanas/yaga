//! Decode bounded PR evidence and retain the exact source boundary used by evaluation.
use crate::json::strict_value;
use crate::timestamp::parse_time;
use regex::Regex;
use serde_json::Value;
use std::{collections::HashSet, path::Path, sync::LazyLock};

pub(super) const MAX_INTEGER: u64 = (1 << 53) - 1;
pub(super) const MAX_BYTES: usize = 1024 * 1024;
pub(super) const REACTIONS: &[&str] = &[
    "+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes",
];
static LOGIN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?$").unwrap()
});
pub(super) fn hex(s: &str, n: usize) -> bool {
    s.len() == n
        && s.bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
pub(super) type Time = i64;

#[derive(Debug)]
pub(super) struct Actor {
    pub login: String,
    pub id: Option<u64>,
}
#[derive(Debug)]
pub(super) struct Review {
    pub id: u64,
    pub actor: Actor,
    pub state: String,
    pub commit: Option<String>,
    pub time: Option<Time>,
    pub body: String,
}
#[derive(Debug)]
pub(super) struct Inline {
    pub id: u64,
    pub actor: Actor,
    pub review: Option<u64>,
    pub commit: Option<String>,
    pub time: Time,
}
#[derive(Debug)]
pub(super) struct Issue {
    pub id: u64,
    pub actor: Actor,
    pub app: Option<u64>,
    pub time: Time,
    pub updated: Option<Time>,
    pub body: String,
}
#[derive(Debug)]
pub(super) struct Reaction {
    pub id: u64,
    pub actor: Actor,
    pub content: String,
    pub time: Time,
}
#[derive(Debug)]
pub(super) struct Run {
    pub id: u64,
    pub name: String,
    pub status: String,
    pub conclusion: Option<String>,
    pub head: String,
    pub app: Option<u64>,
    pub start: Option<Time>,
    pub end: Option<Time>,
}

#[derive(Debug)]
pub struct Snapshot {
    pub(super) number: u64,
    url: Option<String>,
    pub(super) head: String,
    pub(super) base: String,
    pub(super) state: String,
    pub(super) draft: bool,
    pub(super) head_time: Option<Time>,
    pub(super) plan: Option<String>,
    pub(super) reviews: Vec<Review>,
    pub(super) inline: Vec<Inline>,
    pub(super) issues: Vec<Issue>,
    pub(super) reactions: Vec<Reaction>,
    pub(super) runs: Vec<Run>,
    pub(super) requested: Vec<Actor>,
}

struct Fields<'a> {
    value: &'a Value,
    label: String,
    source: &'a str,
}
impl<'a> Fields<'a> {
    fn new(
        value: &'a Value,
        label: String,
        allowed: &[&str],
        source: &'a str,
    ) -> Result<Self, String> {
        let object = value
            .as_object()
            .ok_or_else(|| format!("{label} must be an object in {source}"))?;
        if let Some(k) = object
            .keys()
            .filter(|k| !allowed.contains(&k.as_str()))
            .min()
        {
            return Err(format!("{label} has unknown key {k} in {source}"));
        }
        Ok(Self {
            value,
            label,
            source,
        })
    }
    fn key(&self, k: &str) -> String {
        if self.label == "pull-request review snapshot" {
            k.into()
        } else {
            format!("{}.{k}", self.label)
        }
    }
    fn error(&self, k: &str, why: &str) -> String {
        format!(
            "pull-request review snapshot {} {why} in {}",
            self.key(k),
            self.source
        )
    }
    fn id(&self, k: &str) -> Result<u64, String> {
        self.value[k]
            .as_u64()
            .filter(|n| (1..=MAX_INTEGER).contains(n))
            .ok_or_else(|| self.error(k, "is invalid"))
    }
    fn optional_id(&self, k: &str) -> Result<Option<u64>, String> {
        (!self.value[k].is_null()).then(|| self.id(k)).transpose()
    }
    fn sha(&self, k: &str) -> Result<String, String> {
        self.value[k]
            .as_str()
            .filter(|s| hex(s, 40))
            .map(str::to_owned)
            .ok_or_else(|| self.error(k, "must be a 40-char SHA"))
    }
    fn optional_sha(&self, k: &str) -> Result<Option<String>, String> {
        (!self.value[k].is_null()).then(|| self.sha(k)).transpose()
    }
    fn text(&self, k: &str, max: usize) -> Result<Option<String>, String> {
        if self.value[k].is_null() {
            return Ok(None);
        }
        let s = self.value[k]
            .as_str()
            .filter(|s| {
                !s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                    .is_empty()
                    && !s.contains('\0')
            })
            .ok_or_else(|| self.error(k, "is invalid"))?;
        if s.len() > max {
            return Err(self.error(k, "is oversized"));
        }
        Ok(Some(s.into()))
    }
    fn body(&self) -> Result<String, String> {
        if self.value["body"].is_null() {
            return Ok(String::new());
        }
        let s = self.value["body"]
            .as_str()
            .filter(|s| !s.contains('\0'))
            .ok_or_else(|| self.error("body", "is invalid"))?;
        if s.len() > 65_536 {
            return Err(self.error("body", "exceeds 65536 bytes"));
        }
        Ok(s.into())
    }
    fn actor(&self) -> Result<Actor, String> {
        let login = self.value["author_login"]
            .as_str()
            .filter(|s| LOGIN.is_match(s))
            .ok_or_else(|| self.error("author_login", "is invalid"))?;
        Ok(Actor {
            login: login.into(),
            id: self.optional_id("author_id")?,
        })
    }
    fn choice(&self, k: &str, values: &[&str]) -> Result<String, String> {
        self.value[k]
            .as_str()
            .filter(|s| values.contains(s))
            .map(str::to_owned)
            .ok_or_else(|| format!("{}.{} is invalid in {}", self.label, k, self.source))
    }
    fn time(&self, k: &str) -> Result<Time, String> {
        let raw = self.value[k]
            .as_str()
            .filter(|s| !s.is_empty() && s.chars().count() <= 64)
            .ok_or_else(|| self.error(k, "is invalid"))?;
        match parse_time(raw) {
            Some(Some(time)) => Ok(time),
            Some(None) => Err(self.error(k, "must include a timezone offset")),
            None => Err(self.error(k, "is not a valid ISO-8601 timestamp")),
        }
    }
    fn optional_time(&self, k: &str) -> Result<Option<Time>, String> {
        (!self.value[k].is_null()).then(|| self.time(k)).transpose()
    }
}

fn records<T>(
    root: &Fields<'_>,
    name: &str,
    keys: &[&str],
    parse: impl Fn(&Fields<'_>) -> Result<T, String>,
) -> Result<Vec<T>, String> {
    let Some(value) = root.value.get(name) else {
        return Ok(Vec::new());
    };
    let list = value
        .as_array()
        .ok_or_else(|| root.error(name, "must be an array"))?;
    if list.len() >= 100 {
        return Err(root.error(
            name,
            &format!("has {} records (limit 99); page is incomplete", list.len()),
        ));
    }
    let mut ids = HashSet::new();
    let mut logins = HashSet::new();
    let mut result = Vec::with_capacity(list.len());
    for (index, v) in list.iter().enumerate() {
        let item = Fields::new(v, format!("{name}[{index}]"), keys, root.source)?;
        if keys.contains(&"id") {
            if !ids.insert(item.id("id")?) {
                return Err(format!("{}.id is repeated in {}", item.label, item.source));
            }
        } else if let Some(login) = item.value["author_login"].as_str()
            && !logins.insert(login.to_ascii_lowercase())
        {
            return Err(format!(
                "{}.author_login is repeated in {}",
                item.label, item.source
            ));
        }
        result.push(parse(&item)?);
    }
    Ok(result)
}

impl Snapshot {
    pub fn url(&self) -> Option<&str> {
        self.url.as_deref()
    }
    pub fn number(&self) -> u64 {
        self.number
    }
    pub fn head_sha(&self) -> &str {
        &self.head
    }
    pub fn base_sha(&self) -> &str {
        &self.base
    }
    pub fn state(&self) -> &str {
        &self.state
    }
    pub fn draft(&self) -> bool {
        self.draft
    }
    pub fn load(path: &Path) -> Result<Self, String> {
        let raw = crate::fs::read_regular_prefix(path, MAX_BYTES).map_err(|e| {
            format!(
                "cannot read pull-request review snapshot {}: {e}",
                path.display()
            )
        })?;
        Self::from_bytes(&raw, &path.display().to_string())
    }
    pub fn from_bytes(bytes: &[u8], source: &str) -> Result<Self, String> {
        if bytes.len() > MAX_BYTES {
            return Err(format!(
                "pull-request review snapshot exceeds {MAX_BYTES} bytes: {source}"
            ));
        }
        let doc = strict_value(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes))
            .map_err(|_| format!("pull-request review snapshot is not valid JSON: {source}"))?;
        Self::parse(&doc, source)
    }
    /// Validate one decoded document, preserving the bounds for every evidence inventory.
    pub fn parse(doc: &Value, source: &str) -> Result<Self, String> {
        let root = Fields::new(
            doc,
            "pull-request review snapshot".into(),
            &[
                "version",
                "plan_digest",
                "number",
                "url",
                "state",
                "draft",
                "head_sha",
                "base_sha",
                "head_committed_at",
                "reviews",
                "review_comments",
                "issue_comments",
                "reactions",
                "check_runs",
                "requested_reviewers",
            ],
            source,
        )?;
        if doc["version"].as_u64() != Some(1) {
            return Err(root.error("version", "must be integer 1"));
        }
        let plan = if doc["plan_digest"].is_null() {
            None
        } else {
            Some(
                doc["plan_digest"]
                    .as_str()
                    .filter(|s| hex(s, 64))
                    .ok_or_else(|| root.error("plan_digest", "is invalid"))?
                    .into(),
            )
        };
        let number = root.id("number")?;
        let head = root.sha("head_sha")?;
        let base = root.sha("base_sha")?;
        let state = if doc.get("state").is_none() {
            "open".into()
        } else {
            doc["state"]
                .as_str()
                .filter(|s| matches!(*s, "open" | "closed"))
                .ok_or_else(|| root.error("state", "is invalid"))?
                .into()
        };
        let draft = doc
            .get("draft")
            .map(|v| {
                v.as_bool()
                    .ok_or_else(|| root.error("draft", "must be a boolean"))
            })
            .transpose()?
            .unwrap_or(false);
        let url = root.text("url", 1024)?;
        let head_time = root.optional_time("head_committed_at")?;
        let reviews = records(
            &root,
            "reviews",
            &[
                "id",
                "author_login",
                "author_id",
                "state",
                "commit_id",
                "submitted_at",
                "body",
            ],
            |i| {
                Ok(Review {
                    id: i.id("id")?,
                    actor: i.actor()?,
                    state: i.choice(
                        "state",
                        &[
                            "APPROVED",
                            "CHANGES_REQUESTED",
                            "COMMENTED",
                            "DISMISSED",
                            "PENDING",
                        ],
                    )?,
                    commit: i.optional_sha("commit_id")?,
                    time: i.optional_time("submitted_at")?,
                    body: i.body()?,
                })
            },
        )?;
        let inline = records(
            &root,
            "review_comments",
            &[
                "id",
                "pull_request_review_id",
                "author_login",
                "author_id",
                "commit_id",
                "created_at",
                "path",
                "body",
            ],
            |i| {
                let inline = Inline {
                    id: i.id("id")?,
                    review: i.optional_id("pull_request_review_id")?,
                    actor: i.actor()?,
                    commit: i.optional_sha("commit_id")?,
                    time: i.time("created_at")?,
                };
                i.text("path", 4096)?
                    .ok_or_else(|| i.error("path", "is required"))?;
                i.body()?;
                Ok(inline)
            },
        )?;
        let issues = records(
            &root,
            "issue_comments",
            &[
                "id",
                "author_login",
                "author_id",
                "app_id",
                "created_at",
                "updated_at",
                "body",
            ],
            |i| {
                Ok(Issue {
                    id: i.id("id")?,
                    actor: i.actor()?,
                    app: i.optional_id("app_id")?,
                    time: i.time("created_at")?,
                    updated: i.optional_time("updated_at")?,
                    body: i.body()?,
                })
            },
        )?;
        let reactions = records(
            &root,
            "reactions",
            &["id", "author_login", "author_id", "content", "created_at"],
            |i| {
                Ok(Reaction {
                    id: i.id("id")?,
                    actor: i.actor()?,
                    content: i.choice("content", REACTIONS)?,
                    time: i.time("created_at")?,
                })
            },
        )?;
        let runs = records(
            &root,
            "check_runs",
            &[
                "id",
                "name",
                "status",
                "conclusion",
                "head_sha",
                "app_slug",
                "app_id",
                "started_at",
                "completed_at",
            ],
            |i| {
                let name = i
                    .text("name", 256)?
                    .ok_or_else(|| i.error("name", "is required"))?;
                if name.trim() != name || name.chars().any(|c| c < ' ' || c == '\x7f') {
                    return Err(format!("{}.name is invalid in {source}", i.label));
                }
                let status = i.choice(
                    "status",
                    &[
                        "queued",
                        "in_progress",
                        "completed",
                        "waiting",
                        "requested",
                        "pending",
                    ],
                )?;
                let conclusion = if status == "completed" {
                    Some(i.choice(
                        "conclusion",
                        &[
                            "success",
                            "failure",
                            "neutral",
                            "cancelled",
                            "skipped",
                            "timed_out",
                            "action_required",
                            "stale",
                        ],
                    )?)
                } else if i.value["conclusion"].is_null() {
                    None
                } else {
                    return Err(format!(
                        "{}.conclusion must be null when not completed in {source}",
                        i.label
                    ));
                };
                let head = i.sha("head_sha")?;
                i.text("app_slug", 128)?;
                let app = i.optional_id("app_id")?;
                let start = i.optional_time("started_at")?;
                let end = i.optional_time("completed_at")?;
                if start.zip(end).is_some_and(|(s, e)| e < s) {
                    return Err(format!(
                        "{}.completed_at precedes started_at in {source}",
                        i.label
                    ));
                }
                Ok(Run {
                    id: i.id("id")?,
                    name,
                    status,
                    conclusion,
                    head,
                    app,
                    start,
                    end,
                })
            },
        )?;
        let requested = records(
            &root,
            "requested_reviewers",
            &["author_login", "author_id"],
            |i| i.actor(),
        )?;
        Ok(Self {
            number,
            url,
            head,
            base,
            state,
            draft,
            head_time,
            plan,
            reviews,
            inline,
            issues,
            reactions,
            runs,
            requested,
        })
    }
}
