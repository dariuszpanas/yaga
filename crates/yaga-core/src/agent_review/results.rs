use super::{MAX_BYTES, NAME, Policy};
use crate::json::strict_value;
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, path::Path};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    Passed,
    Failed,
    Pending,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Passed => "passed",
            Self::Failed => "failed",
            Self::Pending => "pending",
        }
    }
}

#[derive(Serialize, Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct LensResult {
    pub lens: String,
    pub outcome: Outcome,
    pub summary: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    version: u8,
    plan_digest: String,
    results: Vec<LensResult>,
}

#[derive(Serialize, Debug)]
pub struct Aggregate {
    pub version: u8,
    pub plan_digest: String,
    pub state: Outcome,
    pub blocking_failures: Vec<String>,
    pub blocking_pending: Vec<String>,
    pub advisory_failures: Vec<String>,
    pub advisory_pending: Vec<String>,
    pub results: Vec<LensResult>,
}

impl Policy {
    /// Validate a complete adapter receipt and evaluate it against this exact plan.
    pub fn evaluate(&self, bytes: &[u8], path: &Path) -> Result<Aggregate, String> {
        let path = path.display();
        if bytes.len() > MAX_BYTES {
            return Err(format!(
                "Agent review results exceed {MAX_BYTES} bytes: {path}"
            ));
        }
        let value = strict_value(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes))
            .map_err(|_| format!("Agent review results are not valid JSON: {path}"))?;
        let receipt: Receipt = serde_json::from_value(value)
            .map_err(|_| format!("Agent review results have invalid fields in {path}"))?;
        if receipt.version != 1 {
            return Err(format!(
                "Agent review results.version must be integer 1 in {path}"
            ));
        }
        self.evaluate_receipt(receipt.plan_digest, receipt.results, &path.to_string())
    }

    /// Evaluate a complete typed result set bound to this exact policy digest.
    pub fn evaluate_receipt(
        &self,
        plan_digest: String,
        results: Vec<LensResult>,
        path: &str,
    ) -> Result<Aggregate, String> {
        if results.len() > 32 {
            return Err(format!(
                "Agent review results.results is unbounded or invalid in {path}"
            ));
        }
        if plan_digest != self.digest() {
            return Err("Agent review results do not match the current policy plan".into());
        }
        let mut outcomes = HashMap::new();
        for item in &results {
            if !NAME.is_match(&item.lens)
                || outcomes.insert(item.lens.as_str(), item.outcome).is_some()
            {
                return Err(format!(
                    "Agent review result lens is invalid or repeated in {path}"
                ));
            }
            if item.summary.as_ref().is_some_and(|s| {
                s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                    .is_empty()
                    || s.contains('\0')
                    || s.len() > 4096
            }) {
                return Err(format!("Agent review result summary is invalid in {path}"));
            }
        }
        let names: Vec<_> = self
            .lenses
            .iter()
            .map(|l| l["name"].as_str().unwrap())
            .collect();
        if let Some(name) = outcomes
            .keys()
            .copied()
            .filter(|name| !names.contains(name))
            .min()
        {
            return Err(format!("Agent review results name unknown lens {name}"));
        }
        let missing: Vec<_> = names
            .iter()
            .copied()
            .filter(|name| !outcomes.contains_key(name))
            .collect();
        if !missing.is_empty() {
            return Err(format!(
                "Agent review results are missing lens(es): {}",
                missing.join(", ")
            ));
        }
        let select = |names: &[&str], state| {
            names
                .iter()
                .filter(|name| outcomes[**name] == state)
                .map(|name| (*name).to_owned())
                .collect::<Vec<_>>()
        };
        let required: Vec<_> = self.required.iter().map(String::as_str).collect();
        let advisory: Vec<_> = names
            .into_iter()
            .filter(|n| !required.contains(n))
            .collect();
        let blocking_failures = select(&required, Outcome::Failed);
        let blocking_pending = select(&required, Outcome::Pending);
        let state = if self.aggregation == "all-required" {
            if !blocking_failures.is_empty() {
                Outcome::Failed
            } else if !blocking_pending.is_empty() {
                Outcome::Pending
            } else {
                Outcome::Passed
            }
        } else if required.iter().any(|n| outcomes[n] == Outcome::Passed) {
            Outcome::Passed
        } else if blocking_pending.is_empty() {
            Outcome::Failed
        } else {
            Outcome::Pending
        };
        Ok(Aggregate {
            version: 1,
            plan_digest,
            state,
            blocking_failures,
            blocking_pending,
            advisory_failures: select(&advisory, Outcome::Failed),
            advisory_pending: select(&advisory, Outcome::Pending),
            results,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_incomplete_displaced_and_duplicate_receipts() {
        let path = Path::new("policy.toml");
        let policy = Policy::parse(
            b"[agent-review]\nversion=1\nrequired=['codex','claude']\naggregation='any-required'",
            path,
        )
        .unwrap();
        let mut receipt = policy.template();
        let run =
            |doc: &serde_json::Value| policy.evaluate(&serde_json::to_vec(doc).unwrap(), path);
        assert_eq!(run(&receipt).unwrap().state, Outcome::Pending);
        receipt["results"][0]["outcome"] = "passed".into();
        assert_eq!(run(&receipt).unwrap().state, Outcome::Passed);
        receipt["results"].as_array_mut().unwrap().pop();
        assert!(run(&receipt).is_err());
        let mut receipt = policy.template();
        receipt["plan_digest"] = "0".repeat(64).into();
        assert!(run(&receipt).is_err());
        let malformed = serde_json::to_string(&policy.template())
            .unwrap()
            .replace("\"version\":1", "\"version\":2,\"version\":1");
        assert!(policy.evaluate(malformed.as_bytes(), path).is_err());
    }
}
