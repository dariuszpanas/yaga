//! Immutable identities and trusted path policy for pull-request trust triage.
use super::{decode_v1, unique};
use crate::paths::{DriveSyntax, Pattern};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashSet;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct TrustedBot {
    pub user_id: u64,
    pub login: String,
    pub allowed_patterns: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "kebab-case", deny_unknown_fields)]
pub struct TriagePolicy {
    pub host: String,
    pub repository_id: u64,
    pub maintainer_user_ids: Vec<u64>,
    #[serde(default)]
    pub trusted_bots: Vec<TrustedBot>,
    #[serde(default = "default_patterns")]
    pub sensitive_patterns: Vec<String>,
    #[serde(default = "default_candidates")]
    pub duplicate_candidates: usize,
    pub comment_author_id: Option<u64>,
    #[serde(skip)]
    pub digest: String,
}
fn default_candidates() -> usize {
    5
}
fn default_patterns() -> Vec<String> {
    [
        ".github/**",
        ".yaga/**",
        ".yaga.toml",
        "**/AGENTS.md",
        "**/CODEOWNERS",
        "**/SECURITY.md",
        "**/CONTRIBUTING.md",
    ]
    .map(String::from)
    .into()
}

fn patterns(values: &[String], key: &str, syntax: DriveSyntax) -> Result<(), String> {
    if !(1..=64).contains(&values.len()) {
        return Err(format!("{key} must contain 1..64 patterns"));
    }
    for pattern in values {
        Pattern::compile_with_drive_syntax(pattern, syntax)
            .map_err(|_| format!("{key} contains an invalid path pattern"))?;
    }
    if !unique(values) {
        return Err(format!("{key} contains duplicate patterns"));
    }
    Ok(())
}
fn id(value: u64, name: &str) -> Result<(), String> {
    if !(1..=(1 << 53) - 1).contains(&value) {
        return Err(format!("{name} must be a positive portable integer"));
    }
    Ok(())
}

impl TriagePolicy {
    pub fn parse(raw: &[u8]) -> Result<Self, String> {
        Self::decode(raw)
    }

    fn decode(raw: &[u8]) -> Result<Self, String> {
        if raw.len() > 65536 {
            return Err("pull-request triage policy exceeds 64 KiB".into());
        }
        let mut p: Self = decode_v1(raw, "triage-policy-version", "pull-request triage policy")?;
        p.validate(DriveSyntax::NATIVE)?;
        p.digest = format!("{:x}", Sha256::digest(raw));
        Ok(p)
    }

    /// Validate resolved policy fields under the caller's path grammar.
    pub fn validate(&self, syntax: DriveSyntax) -> Result<(), String> {
        if !self.host.is_ascii()
            || self.host.len() > 253
            || self.host != self.host.to_ascii_lowercase()
            || !self.host.split('.').all(|part| {
                part.as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                    && part
                        .as_bytes()
                        .last()
                        .is_some_and(u8::is_ascii_alphanumeric)
                    && part.bytes().all(|b| b == b'-' || b.is_ascii_alphanumeric())
            })
        {
            return Err("triage host must be one lowercase DNS hostname".into());
        }
        id(self.repository_id, "repository-id")?;
        if self.maintainer_user_ids.len() > 32 {
            return Err("maintainer-user-ids must contain at most 32 IDs".into());
        }
        for &v in &self.maintainer_user_ids {
            id(v, "maintainer-user-ids")?;
        }
        if !unique(&self.maintainer_user_ids) {
            return Err("maintainer-user-ids contains duplicate IDs".into());
        }
        let mut ids: HashSet<_> = self.maintainer_user_ids.iter().copied().collect();
        if self.trusted_bots.len() > 32 {
            return Err("trusted-bots must contain at most 32 records".into());
        }
        for bot in &self.trusted_bots {
            id(bot.user_id, "trusted-bots.user-id")?;
            if !(1..=100).contains(&bot.login.len())
                || !bot
                    .login
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-[]".contains(&b))
            {
                return Err("trusted bot login is invalid".into());
            }
            patterns(&bot.allowed_patterns, "bot allowed-patterns", syntax)?;
            if !ids.insert(bot.user_id) {
                return Err("triage identities overlap or repeat".into());
            }
        }
        if self.duplicate_candidates > 20 {
            return Err("duplicate-candidates must be an integer in 0..20".into());
        }
        patterns(&self.sensitive_patterns, "sensitive-patterns", syntax)?;
        if let Some(v) = self.comment_author_id {
            id(v, "comment-author-id")?;
        }
        Ok(())
    }
}
