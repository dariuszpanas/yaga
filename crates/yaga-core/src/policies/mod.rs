//! Repository policy schema registry, starter generation and version auditing.
mod audit;
mod init;
pub mod selector;
pub mod testing;
#[cfg(test)]
mod tests;
pub mod triage;
use crate::{
    agent_review, branch, commits, descriptions,
    paths::{changes, modes, portability, sizes, tree},
    repository,
    text::safe_text,
    version::*,
};
pub use audit::{AuditReport, Format, audit_policies, classify_policy};
pub use init::{
    InitConflict, InitReport, InitSelection, PreparedPolicy, existing_policies,
    initialize_policies, render_starter,
};
use serde::{Serialize, de::DeserializeOwned};
use std::{collections::HashSet, fmt, hash::Hash, path::Path};
use toml::{Table, Value};

macro_rules! kinds {
    ($($variant:ident, $key:literal,$path:literal,$version:literal,$current:literal,$desc:literal;)+) => {
        #[derive(Clone,Copy,Debug,Eq,PartialEq,Hash,Serialize)]
        pub enum Kind { $(#[serde(rename=$key)]$variant),+ }
        impl Kind {
            pub const ALL:&[Self]=&[$(Self::$variant),+];
            pub fn as_str(self)->&'static str {match self {$(Self::$variant=>$key),+}}
            pub fn default_path(self)->&'static str {match self {$(Self::$variant=>$path),+}}
            pub fn version_key(self)->&'static str {match self {$(Self::$variant=>$version),+}}
            pub fn current(self)->i64 {match self {$(Self::$variant=>$current),+}}
            pub fn description(self)->&'static str {match self {$(Self::$variant=>$desc),+}}
        }
        impl std::str::FromStr for Kind {
            type Err=String;
            fn from_str(s:&str)->Result<Self,String>{ match s {$($key=>Ok(Self::$variant),)+ _=>Err("unknown policy family".into())} }
        }
    }
}
include!(concat!(env!("OUT_DIR"), "/policy_catalog.rs"));
pub const TREE_KINDS: [Kind; 4] = [Kind::Mode, Kind::Path, Kind::Size, Kind::Tree];

#[derive(Debug)]
pub enum Error {
    Input(String),
    Configuration(String),
}
impl Error {
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Input(_) => "input",
            Self::Configuration(_) => "configuration",
        }
    }
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Input(s) | Self::Configuration(s) => f.write_str(s),
        }
    }
}
impl std::error::Error for Error {}

pub(crate) fn has_datetime(t: &Table) -> bool {
    let mut todo: Vec<_> = t.values().collect();
    while let Some(v) = todo.pop() {
        match v {
            Value::Datetime(_) => return true,
            Value::Array(a) => todo.extend(a),
            Value::Table(t) => todo.extend(t.values()),
            _ => (),
        }
    }
    false
}

fn decode_v1<T: DeserializeOwned>(raw: &[u8], version: &str, kind: &str) -> Result<T, String> {
    let text = std::str::from_utf8(raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw))
        .map_err(|_| format!("{kind} must be valid UTF-8"))?;
    let mut t: toml::Table = toml::from_str(text).map_err(|_| format!("invalid {kind} TOML"))?;
    if let Some(v) = t.remove("required-version") {
        check_requirement(v.as_str().ok_or(REQUIREMENT_TYPE_ERROR)?)?;
    }
    let actual = t.remove(version).and_then(|v| v.as_integer());
    if actual != Some(1) {
        return Err(format!("invalid {kind}: {version} must be the integer 1"));
    }
    if has_datetime(&t) {
        return Err(format!(
            "invalid {kind}: TOML datetime values are unsupported"
        ));
    }
    Value::Table(t)
        .try_into()
        .map_err(|e| format!("invalid {kind}: {}", safe_text(e.message(), 500)))
}

fn unique<T: Eq + Hash>(items: &[T]) -> bool {
    items.iter().collect::<HashSet<_>>().len() == items.len()
}

pub fn validate(kind: Kind, raw: &[u8], path: &Path) -> Result<i64, String> {
    match kind {
        Kind::Commit => {
            commits::parse_config(raw, path)?;
        }
        Kind::Repo => return repository::parse_plan(raw, path).map(|p| i64::from(p.version)),
        Kind::Branch => {
            branch::parse_branch_policy(raw, path).map_err(|e| e.to_string())?;
        }
        Kind::Change => {
            changes::ChangePolicy::from_toml(raw).map_err(|e| e.to_string())?;
        }
        Kind::Description => {
            descriptions::policy::parse_description_policy(raw)?;
        }
        Kind::Mode => {
            modes::ModePolicy::from_toml(raw).map_err(|e| e.to_string())?;
        }
        Kind::Path => {
            portability::PathPolicy::from_toml(raw).map_err(|e| e.to_string())?;
        }
        Kind::Size => {
            sizes::SizePolicy::from_toml(raw).map_err(|e| e.to_string())?;
        }
        Kind::Tree => {
            tree::TreePolicy::from_toml(raw).map_err(|e| e.to_string())?;
        }
        Kind::AgentReview => {
            agent_review::Policy::parse(raw, path)?;
        }
        Kind::Test => {
            testing::TestPolicy::parse(raw)?;
        }
        Kind::Triage => {
            triage::TriagePolicy::parse(raw)?;
        }
    }
    Ok(1)
}

pub fn pretty_json(obj: &serde_json::Value) -> String {
    use std::fmt::Write;
    let json = serde_json::to_string_pretty(obj).expect("policy report is JSON");
    let mut ascii = String::new();
    for ch in json.chars() {
        if ch < '\u{007f}' {
            ascii.push(ch);
        } else {
            for unit in ch.encode_utf16(&mut [0; 2]).iter() {
                write!(ascii, "\\u{unit:04x}").unwrap();
            }
        }
    }
    ascii
}
