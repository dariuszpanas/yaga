//! Strict qualification policy. Bindings and capabilities are opaque names.
use super::{decode_v1, unique};
use crate::paths::Pattern;
use regex::Regex;
use serde::{Deserialize, Deserializer, Serialize};
use sha2::{Digest, Sha256};
use std::{collections::HashSet, sync::LazyLock};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Suite {
    pub id: String,
    pub binding: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_variants")]
    pub variants: Vec<String>,
    #[serde(default)]
    pub needs: Vec<String>,
    #[serde(default)]
    pub requires: Vec<String>,
}
fn default_variants() -> Vec<String> {
    vec!["default".into()]
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    pub mode: Mode,
    #[serde(default)]
    pub always: Vec<String>,
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Selective,
    Full,
}
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum UnknownPaths {
    #[default]
    Full,
    Error,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct Rule {
    pub id: String,
    pub when_any: Vec<String>,
    #[serde(default)]
    pub select: Vec<String>,
    #[serde(default)]
    pub select_tags: Vec<String>,
}
#[derive(Debug, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct TestPolicy {
    pub suites: Vec<Suite>,
    #[serde(deserialize_with = "profiles")]
    pub profiles: Vec<(String, Profile)>,
    #[serde(default)]
    pub rules: Vec<Rule>,
    #[serde(default)]
    pub unknown_paths: UnknownPaths,
    #[serde(default)]
    pub full_on: Vec<String>,
    #[serde(default)]
    pub no_impact: Vec<String>,
    #[serde(skip)]
    pub sha256: String,
}

fn profiles<'de, D: Deserializer<'de>>(d: D) -> Result<Vec<(String, Profile)>, D::Error> {
    let map = toml::Table::deserialize(d)?;
    map.into_iter()
        .map(|(k, v)| {
            v.try_into()
                .map(|p| (k, p))
                .map_err(serde::de::Error::custom)
        })
        .collect()
}
pub(crate) fn token(s: &str, key: &str) -> Result<(), String> {
    static TOKEN: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\A[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\z").unwrap());
    if s.len() > 64 || !TOKEN.is_match(s) {
        return Err(format!(
            "{key} must be a 1 through 64 byte lowercase ASCII token"
        ));
    }
    Ok(())
}
pub(crate) fn tokens(v: &[String], key: &str, min: usize, max: usize) -> Result<(), String> {
    if !(min..=max).contains(&v.len()) {
        return Err(format!(
            "{key} requires {min} through {max} immutable entries"
        ));
    }
    for s in v {
        token(s, key)?;
    }
    if !unique(v) {
        return Err(format!("{key} must be unique"));
    }
    Ok(())
}
fn patterns(v: &[String], key: &str, min: usize) -> Result<(), String> {
    if !(min..=128).contains(&v.len()) {
        return Err(format!(
            "{key} requires {min} through 128 immutable patterns"
        ));
    }
    for s in v {
        Pattern::compile(s).map_err(|e| e.to_string())?;
    }
    if !unique(v) {
        return Err(format!("{key} must be unique"));
    }
    Ok(())
}

impl TestPolicy {
    pub fn parse(raw: &[u8]) -> Result<Self, String> {
        Self::decode(raw)
    }

    fn decode(raw: &[u8]) -> Result<Self, String> {
        if raw.len() > 128 * 1024 {
            return Err("test policy exceeds 131072 bytes".into());
        }
        let mut p: Self = decode_v1(raw, "test-policy-version", "test policy")?;
        p.validate()
            .map_err(|s| format!("invalid test policy: {s}"))?;
        p.sha256 = format!("{:x}", Sha256::digest(raw));
        Ok(p)
    }

    pub(crate) fn validate(&self) -> Result<(), String> {
        if !(1..=128).contains(&self.suites.len()) {
            return Err("suites requires 1 through 128 immutable Suite entries".into());
        }
        if !(1..=16).contains(&self.profiles.len()) {
            return Err("profiles requires 1 through 16 immutable Profile entries".into());
        }
        if self.rules.len() > 128 {
            return Err("rules requires 0 through 128 immutable SelectionRule entries".into());
        }
        let mut ids = HashSet::new();
        let mut tags = HashSet::new();
        let mut capabilities = HashSet::new();
        let mut cells = 0;
        let mut edges = 0;
        for s in &self.suites {
            token(&s.id, "suite id")?;
            token(&s.binding, "suite binding")?;
            tokens(&s.tags, "suite tags", 0, 16)?;
            tokens(&s.variants, "suite variants", 1, 16)?;
            tokens(&s.needs, "suite needs", 0, 16)?;
            tokens(&s.requires, "suite requires", 0, 16)?;
            if s.needs.contains(&s.id) {
                return Err("suite needs must not include its own id".into());
            }
            if !ids.insert(&s.id) {
                return Err("suite ids must be unique".into());
            }
            tags.extend(&s.tags);
            capabilities.extend(&s.requires);
            cells += s.variants.len();
            edges += s.needs.len();
        }
        if cells > 512 {
            return Err("catalog exceeds 512 expanded variant cells".into());
        }
        if edges > 512 {
            return Err("catalog exceeds 512 prerequisite edges".into());
        }
        if tags.len() > 128 {
            return Err("catalog exceeds 128 distinct tags".into());
        }
        if capabilities.len() > 128 {
            return Err("catalog exceeds 128 distinct capabilities".into());
        }
        for (name, p) in &self.profiles {
            token(name, "profile name")?;
            tokens(&p.always, "profile always", 0, 128)?;
            if p.always.iter().any(|s| !ids.contains(s)) {
                return Err("profile always references an unknown suite id".into());
            }
        }
        let mut rules = HashSet::new();
        patterns(&self.full_on, "full-on", 0)?;
        patterns(&self.no_impact, "no-impact", 0)?;
        let mut distinct: HashSet<_> = self.full_on.iter().chain(&self.no_impact).collect();
        for r in &self.rules {
            token(&r.id, "rule id")?;
            patterns(&r.when_any, "rule when-any", 1)?;
            tokens(&r.select, "rule select", 0, 128)?;
            tokens(&r.select_tags, "rule select-tags", 0, 128)?;
            if r.select.is_empty() && r.select_tags.is_empty() {
                return Err(
                    "rule requires a nonempty combined select and select-tags selection".into(),
                );
            }
            if !rules.insert(&r.id) {
                return Err("rule ids must be unique".into());
            }
            if r.select.iter().any(|s| !ids.contains(s)) {
                return Err("rule select references an unknown suite id".into());
            }
            if r.select_tags.iter().any(|s| !tags.contains(s)) {
                return Err(
                    "rule select-tags references an unknown tag with empty expansion".into(),
                );
            }
            distinct.extend(&r.when_any);
        }
        if distinct.len() > 1024 {
            return Err("policy exceeds 1024 distinct patterns".into());
        }
        if self
            .suites
            .iter()
            .flat_map(|s| &s.needs)
            .any(|id| !ids.contains(id))
        {
            return Err("suite needs references an unknown suite id".into());
        }
        let mut ready = HashSet::new();
        while ready.len() < ids.len() {
            let previous = ready.len();
            for s in &self.suites {
                if s.needs.iter().all(|id| ready.contains(id)) {
                    ready.insert(&s.id);
                }
            }
            if previous == ready.len() {
                return Err("suite needs contains a prerequisite cycle".into());
            }
        }
        Ok(())
    }
}
