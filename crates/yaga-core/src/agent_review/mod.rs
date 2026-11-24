//! Offline review-lens policies, plans, and digest-bound adapter receipts.

pub mod github;
mod results;
pub use results::{Aggregate, LensResult, Outcome};

use crate::version::{REQUIREMENT_TYPE_ERROR, check_requirement};
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashSet, path::Path, sync::LazyLock};
use toml::Table;

pub const MAX_BYTES: usize = 1024 * 1024;
static NAME: LazyLock<Regex> = LazyLock::new(|| Regex::new("^[a-z][a-z0-9-]{0,63}$").unwrap());
static LOGIN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?(?:\[bot\])?$").unwrap()
});
const REACTIONS: [&str; 8] = [
    "+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes",
];

#[derive(Debug)]
pub struct Policy {
    required: Vec<String>,
    aggregation: String,
    lenses: Vec<Value>,
}

fn builtin(name: &str) -> bool {
    matches!(name, "codex" | "copilot" | "claude")
}

fn validate_keys(table: &Table, allowed: &[&str], label: &str, path: &str) -> Result<(), String> {
    if let Some(key) = table
        .keys()
        .filter(|s| !allowed.contains(&s.as_str()))
        .min()
    {
        return Err(format!("{label} has unknown key {key} in {path}"));
    }
    Ok(())
}

fn requirement(t: &Table) -> Result<(), String> {
    if let Some(value) = t.get("required-version") {
        check_requirement(value.as_str().ok_or(REQUIREMENT_TYPE_ERROR)?)?;
    }
    Ok(())
}

fn strings(
    value: Option<&toml::Value>,
    min: usize,
    max: usize,
    valid: impl Fn(&str) -> bool,
) -> Option<Vec<String>> {
    let values = value?.as_array()?;
    if !(min..=max).contains(&values.len()) {
        return None;
    }
    let mut seen = HashSet::new();
    values
        .iter()
        .map(|item| {
            let text = item.as_str()?;
            (valid(text) && seen.insert(text)).then(|| text.to_owned())
        })
        .collect()
}

fn lens(name: &str, t: &Table, required: bool, path: &str) -> Result<Value, String> {
    let label = format!("agent-review.agents.{name}");
    let invalid = |key: &str| format!("{label}.{key} is invalid in {path}");
    validate_keys(
        t,
        &[
            "preset",
            "outcome",
            "publication",
            "bot-login",
            "bot-user-id",
            "bot-app-id",
            "pass-reactions",
            "pending-reactions",
            "check-runs",
            "delay-seconds",
            "wait-seconds",
        ],
        &label,
        path,
    )?;
    let string = |key: &str, default: &str| -> Result<String, String> {
        match t.get(key) {
            None => Ok(default.to_owned()),
            Some(v) => v.as_str().map(str::to_owned).ok_or_else(|| invalid(key)),
        }
    };
    let preset = string("preset", if builtin(name) { name } else { "custom" })?;
    let outcome = string("outcome", "review")?;
    let publication = string("publication", "review")?;
    if !NAME.is_match(&preset) {
        return Err(invalid("preset"));
    }
    if !matches!(outcome.as_str(), "review" | "advisory") {
        return Err(invalid("outcome"));
    }
    if !matches!(
        publication.as_str(),
        "comment" | "inline" | "reaction" | "review" | "check"
    ) {
        return Err(invalid("publication"));
    }
    let mut doc = json!({"name":name,"preset":preset,"required":required,"outcome":outcome,"publication":publication});
    if let Some(v) = t.get("bot-login") {
        let login = v
            .as_str()
            .filter(|s| LOGIN.is_match(s))
            .ok_or_else(|| invalid("bot-login"))?;
        doc["bot_login"] = json!(login);
    } else if !builtin(name) && !t.contains_key("preset") {
        return Err(format!("{label} requires preset or bot-login in {path}"));
    }
    for key in ["bot-user-id", "bot-app-id"] {
        if let Some(v) = t.get(key) {
            let n = v
                .as_integer()
                .filter(|x| (1..=(1 << 53) - 1).contains(x))
                .ok_or_else(|| invalid(key))?;
            doc[key.replace('-', "_")] = json!(n);
        }
    }
    for (key, min) in [("pass-reactions", 1), ("pending-reactions", 0)] {
        if t.contains_key(key) {
            let values = strings(t.get(key), min, 8, |s| REACTIONS.contains(&s))
                .ok_or_else(|| invalid(key))?;
            doc[key.replace('-', "_")] = json!(values);
        }
    }
    let pass = t.get("pass-reactions").and_then(toml::Value::as_array);
    let pending = t.get("pending-reactions").and_then(toml::Value::as_array);
    if REACTIONS.iter().any(|s| {
        pass.map_or(*s == "+1", |a| a.iter().any(|v| v.as_str() == Some(s)))
            && pending.map_or(*s == "eyes", |a| a.iter().any(|v| v.as_str() == Some(s)))
    }) {
        return Err(format!(
            "{label} pass-reactions and pending-reactions overlap in {path}"
        ));
    }
    if t.contains_key("check-runs") {
        let runs = strings(t.get("check-runs"), 1, 16, |s| {
            !s.is_empty()
                && s.chars().count() <= 128
                && s.trim() == s
                && !s.chars().any(|c| c.is_ascii_control())
        })
        .ok_or_else(|| invalid("check-runs"))?;
        doc["check_runs"] = json!(runs);
    }
    for (key, max) in [("delay-seconds", 300), ("wait-seconds", 600)] {
        if let Some(v) = t.get(key) {
            let n = v
                .as_integer()
                .filter(|n| (0..=max).contains(n))
                .ok_or_else(|| invalid(key))?;
            if n > 0 {
                doc[key.replace('-', "_")] = json!(n);
            }
        }
    }
    if required && outcome != "review" {
        return Err(format!(
            "agent-review.required lens {name} must have outcome review"
        ));
    }
    Ok(doc)
}

impl Policy {
    pub(crate) fn lenses(&self) -> &[Value] {
        &self.lenses
    }
    pub fn aggregation(&self) -> &str {
        &self.aggregation
    }
    /// Parse an explicit UTF-8/BOM TOML source in either supported layout.
    pub fn parse(bytes: &[u8], path: &Path) -> Result<Self, String> {
        let display = path.display().to_string();
        if bytes.len() > MAX_BYTES {
            return Err(format!(
                "Agent review policy exceeds {MAX_BYTES} bytes: {display}"
            ));
        }
        let text = std::str::from_utf8(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes))
            .map_err(|_| format!("Agent review policy is not valid UTF-8: {display}"))?;
        let t: Table = toml::from_str(text).map_err(|e| {
            format!(
                "invalid Agent review policy TOML in {display}: {}",
                e.message()
            )
        })?;
        Self::from_document(&t, path)
    }

    /// Validate and compile an already decoded versioned policy document.
    pub fn from_document(t: &Table, path: &Path) -> Result<Self, String> {
        let display = path.display().to_string();
        let root = if path.file_name().is_some_and(|p| p == "pyproject.toml") {
            t.get("tool")
                .and_then(|v| v.get("yaga"))
                .and_then(toml::Value::as_table)
                .ok_or_else(|| format!("{display} has no [tool.yaga] table"))?
        } else {
            t
        };
        let policy = root
            .get("agent-review")
            .and_then(toml::Value::as_table)
            .ok_or_else(|| format!("{display} has no [agent-review] table"))?;
        requirement(root)?;
        requirement(policy)?;
        validate_keys(
            policy,
            &[
                "version",
                "required-version",
                "required",
                "aggregation",
                "agents",
            ],
            "agent-review",
            &display,
        )?;
        let version = policy.get("version").and_then(toml::Value::as_integer);
        if version != Some(1) {
            let mut error = format!("agent-review.version must be integer 1 in {display}");
            if let Some(v) = version.filter(|v| *v > 1) {
                error.push_str(&format!(
                    " (found newer schema version {v}; update YAGA using its installation manager)"
                ));
            }
            return Err(error);
        }
        let aggregation = match policy.get("aggregation") {
            None => "all-required",
            Some(v) => v
                .as_str()
                .filter(|s| matches!(*s, "all-required" | "any-required"))
                .ok_or_else(|| format!("agent-review.aggregation is invalid in {display}"))?,
        }
        .to_owned();
        let required=strings(policy.get("required"),1,32,|s|NAME.is_match(s)).ok_or_else(||format!("agent-review.required must be a nonempty bounded array of unique lens names in {display}"))?;
        let mut lenses = Vec::new();
        if let Some(v) = policy.get("agents") {
            let table = v.as_table().filter(|t| !t.is_empty()).ok_or_else(|| {
                format!("agent-review.agents must be a nonempty table in {display}")
            })?;
            if table.len() > 32 {
                return Err(format!(
                    "agent-review.agents exceeds 32 entries in {display}"
                ));
            }
            for (name, v) in table {
                if !NAME.is_match(name) {
                    return Err(format!("agent-review lens name is invalid in {display}"));
                }
                let lens_table = v.as_table().ok_or_else(|| {
                    format!("agent-review.agents.{name} must be a table in {display}")
                })?;
                lenses.push(lens(name, lens_table, required.contains(name), &display)?);
            }
        }
        for name in &required {
            if lenses.iter().all(|v| v["name"] != *name) {
                if !builtin(name) {
                    return Err(format!(
                        "agent-review.required names unknown lens {name} in {display}"
                    ));
                }
                lenses.push(lens(name, &Table::new(), true, &display)?);
            }
        }
        if lenses.len() > 32 {
            return Err(format!(
                "agent-review.agents exceeds 32 entries in {display}"
            ));
        }
        Ok(Self {
            required,
            aggregation,
            lenses,
        })
    }

    fn base(&self) -> Value {
        json!({"version":1,"aggregation":self.aggregation,"lenses":self.lenses})
    }
    pub fn digest(&self) -> String {
        let mut doc = self.base();
        doc.sort_all_objects();
        format!("{:x}", Sha256::digest(serde_json::to_vec(&doc).unwrap()))
    }
    pub fn plan(&self) -> Value {
        let mut doc = self.base();
        doc["plan_digest"] = json!(self.digest());
        doc
    }
    pub fn check(&self) -> Value {
        json!({"version":1,"status":"passed","aggregation":self.aggregation,"lens_count":self.lenses.len(),"required_count":self.required.len(),"required":self.required,"plan_digest":self.digest()})
    }
    pub fn template(&self) -> Value {
        let results: Vec<_> = self
            .lenses
            .iter()
            .map(|v| json!({"lens":v["name"],"outcome":"pending"}))
            .collect();
        json!({"version":1,"plan_digest":self.digest(),"results":results})
    }
}
