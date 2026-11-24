//! Evaluate bounded external receipts against independent policy, source and run identities.
use super::*;
use crate::policies::testing::token;
use serde_json::json;
use std::{collections::BTreeMap, fmt::Write};

pub struct Expected<'a> {
    pub policy_sha256: &'a str,
    pub bindings_sha256: &'a str,
    pub execution_commit: &'a str,
    pub execution_tree: &'a str,
    pub run_id: &'a str,
    pub attempt: u32,
}
fn valid_run(s: &str, a: u32) -> bool {
    (1..=128).contains(&s.len())
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
        && (1..=i32::MAX as u32).contains(&a)
}
pub fn parse_attempt(s: &str) -> Result<u32, Error> {
    if s.len() <= 10
        && s.bytes().all(|b| b.is_ascii_digit())
        && let Ok(n) = s.parse::<u32>()
        && (1..=i32::MAX as u32).contains(&n)
    {
        return Ok(n);
    }
    Err(Error::Input(
        "test attempt must be an integer from 1 through 2147483647".into(),
    ))
}
#[derive(Clone, Copy, Debug, Deserialize, Serialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    Passed,
    Failed,
    Pending,
    Cancelled,
    Blocked,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Cell {
    pub suite: String,
    pub variant: String,
    pub binding: String,
    pub outcome: Outcome,
    #[serde(
        default,
        deserialize_with = "present_string",
        skip_serializing_if = "Option::is_none"
    )]
    pub summary: Option<String>,
    #[serde(
        default,
        deserialize_with = "present_string",
        skip_serializing_if = "Option::is_none"
    )]
    pub evidence_sha256: Option<String>,
}
fn present_string<'de, D: serde::Deserializer<'de>>(de: D) -> Result<Option<String>, D::Error> {
    String::deserialize(de).map(Some)
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
/// Provider-declared version-one receipt. Validate it before encoding or evaluation.
pub struct Receipt {
    #[serde(rename = "test-results-version")]
    pub version: u8,
    pub plan_digest: String,
    pub policy_sha256: String,
    pub bindings_sha256: String,
    pub execution_commit: String,
    pub execution_tree: String,
    pub run_id: String,
    pub attempt: u32,
    pub results: Vec<Cell>,
}
/// Validated external receipts, ordered by suite and variant.
pub struct Results(Receipt);

impl Results {
    /// Return the version-one document with absent optional fields omitted.
    pub fn document(&self) -> Value {
        serde_json::to_value(&self.0).expect("receipt fields are JSON values")
    }

    /// SHA-256 used by the evaluation report, excluding the transport newline.
    pub fn sha256(&self) -> String {
        format!("{:x}", Sha256::digest(self.canonical_json().as_bytes()))
    }

    fn canonical_json(&self) -> String {
        canonical(self.document())
    }

    /// Encode compact UTF-8 JSON with sorted keys and a final line feed.
    pub fn encode(&self) -> Result<Vec<u8>, Error> {
        self.encode_with_limit(MAX_BYTES)
    }

    /// Apply a tighter consumer limit, including the final line feed.
    pub fn encode_with_limit(&self, maximum: usize) -> Result<Vec<u8>, Error> {
        let maximum = maximum.min(MAX_BYTES);
        let mut bytes = self.canonical_json().into_bytes();
        if bytes.len() >= maximum {
            return Err(Error::Configuration(format!(
                "test result report exceeds the hard {maximum}-byte limit"
            )));
        }
        bytes.push(b'\n');
        Ok(bytes)
    }
}
fn valid_execution(commit: &str, tree: &str) -> bool {
    decode::valid_oid(commit) && decode::valid_oid(tree) && commit.len() == tree.len()
}
pub fn parse_results(v: Value) -> Result<Results, Error> {
    let invalid = || Error::Input("invalid version-one test result semantics".into());
    let fields =
        || Error::Input("test results require exactly their versioned JSON object fields".into());
    let keys = [
        "test-results-version",
        "plan_digest",
        "policy_sha256",
        "bindings_sha256",
        "execution_commit",
        "execution_tree",
        "run_id",
        "attempt",
        "results",
    ];
    if !v
        .as_object()
        .is_some_and(|o| o.len() == keys.len() && keys.iter().all(|k| o.contains_key(*k)))
    {
        return Err(fields());
    }
    let cells = v["results"]
        .as_array()
        .filter(|c| c.len() <= 512)
        .ok_or_else(|| Error::Input("test results require a bounded JSON array of cells".into()))?;
    for c in cells {
        let required = ["suite", "variant", "binding", "outcome"];
        let ok = c.as_object().is_some_and(|o| {
            required.iter().all(|k| o.contains_key(*k))
                && o.keys().all(|k| {
                    required.contains(&k.as_str())
                        || matches!(k.as_str(), "summary" | "evidence_sha256")
                })
        });
        if !ok {
            return Err(fields());
        }
        if ["summary", "evidence_sha256"]
            .iter()
            .any(|&k| c.get(k).is_some_and(|v| !v.is_string()))
        {
            return Err(Error::Input(
                "test result field has an invalid type or exceeds its string limit".into(),
            ));
        }
    }
    let r: Receipt = serde_json::from_value(v).map_err(|_| invalid())?;
    r.try_into()
}

impl TryFrom<Receipt> for Results {
    type Error = Error;

    fn try_from(mut r: Receipt) -> Result<Self, Error> {
        let invalid = || Error::Input("invalid version-one test result semantics".into());
        if r.results.len() > 512 {
            return Err(Error::Input(
                "test results require a bounded JSON array of cells".into(),
            ));
        }
        if r.version != 1
            || [&r.plan_digest, &r.policy_sha256, &r.bindings_sha256]
                .iter()
                .any(|s| !is_digest(s))
            || !valid_execution(&r.execution_commit, &r.execution_tree)
            || !valid_run(&r.run_id, r.attempt)
        {
            return Err(invalid());
        }
        for c in &r.results {
            if [&c.suite, &c.binding, &c.variant]
                .iter()
                .any(|s| token(s, "cell").is_err())
                || c.summary.as_ref().is_some_and(|s| s.len() > 1000)
                || c.evidence_sha256.as_deref().is_some_and(|s| !is_digest(s))
            {
                return Err(invalid());
            }
        }
        r.results
            .sort_by(|a, b| (&a.suite, &a.variant).cmp(&(&b.suite, &b.variant)));
        if r.results
            .windows(2)
            .any(|w| w[0].suite == w[1].suite && w[0].variant == w[1].variant)
        {
            return Err(invalid());
        }
        Ok(Results(r))
    }
}
#[derive(Default, Serialize)]
struct Counts {
    selected_cells: usize,
    received_cells: usize,
    passed: usize,
    failed: usize,
    pending: usize,
    cancelled: usize,
    blocked: usize,
    missing: usize,
}
#[derive(Serialize)]
struct Diagnostic {
    code: &'static str,
    suite: String,
    variant: String,
    binding: String,
    outcome: Option<Outcome>,
    message: &'static str,
}

pub struct Evaluation {
    pub passed: bool,
    pub json: String,
    pub text: String,
}
pub fn evaluate(
    doc: &PlanDocument,
    receipt: &Results,
    expected: Expected<'_>,
) -> Result<Evaluation, Error> {
    let results = &receipt.0;
    let e = expected;
    if !is_digest(e.policy_sha256)
        || !is_digest(e.bindings_sha256)
        || !valid_execution(e.execution_commit, e.execution_tree)
        || !valid_run(e.run_id, e.attempt)
    {
        return Err(Error::Input(
            "invalid test evaluation model or expected identity".into(),
        ));
    }
    doc.validate()?;
    let p = &doc.plan;
    let Some(bindings) = &p.bindings_sha256 else {
        return Err(Error::Input(
            "preview test plans without a binding identity cannot qualify".into(),
        ));
    };
    for (ok, message) in [
        (
            p.policy_sha256 == e.policy_sha256 && results.policy_sha256 == e.policy_sha256,
            "test evaluation policy identity does not match the expected policy",
        ),
        (
            bindings == e.bindings_sha256 && results.bindings_sha256 == e.bindings_sha256,
            "test evaluation binding identity does not match the expected manifest",
        ),
        (
            p.source.execution_commit == e.execution_commit
                && results.execution_commit == e.execution_commit
                && p.source.execution_tree == e.execution_tree
                && results.execution_tree == e.execution_tree,
            "test evaluation source does not match the expected execution identity",
        ),
        (
            results.run_id == e.run_id && results.attempt == e.attempt,
            "test evaluation run or aggregate attempt does not match the expected identity",
        ),
        (
            results.plan_digest == doc.plan_digest,
            "test result plan digest does not match the expected plan",
        ),
    ] {
        if !ok {
            return Err(Error::Input(message.into()));
        }
    }
    let catalog: BTreeMap<_, _> = p
        .selection
        .inventory
        .iter()
        .map(|s| (&s.id, &s.binding))
        .collect();
    let mut required = BTreeMap::new();
    for s in &p.selection.selected {
        for variant in &s.variants {
            required.insert((&s.suite, variant), catalog[&s.suite]);
        }
    }
    let mut received = BTreeMap::new();
    for c in &results.results {
        if required.get(&(&c.suite, &c.variant)) != Some(&&c.binding) {
            return Err(Error::Input(
                "test results contain an unknown, unselected, or incorrectly bound cell".into(),
            ));
        }
        received.insert((&c.suite, &c.variant), c.outcome);
    }
    let mut n = Counts {
        selected_cells: required.len(),
        received_cells: received.len(),
        ..Counts::default()
    };
    let mut diagnostics = Vec::new();
    for ((suite, variant), binding) in required {
        let outcome = received.get(&(suite, variant)).copied();
        let (code, message) = match outcome {
            Some(Outcome::Passed) => {
                n.passed += 1;
                continue;
            }
            Some(Outcome::Failed) => {
                n.failed += 1;
                ("test.failed", "Required cell failed.")
            }
            Some(Outcome::Cancelled) => {
                n.cancelled += 1;
                ("test.cancelled", "Required cell was cancelled.")
            }
            Some(Outcome::Pending) => {
                n.pending += 1;
                ("test.required-result", "Required cell is pending.")
            }
            Some(Outcome::Blocked) => {
                n.blocked += 1;
                ("test.required-result", "Required cell is blocked.")
            }
            None => {
                n.missing += 1;
                ("test.required-result", "Required cell has no result.")
            }
        };
        if diagnostics.len() < 256 {
            diagnostics.push(Diagnostic {
                code,
                message,
                suite: suite.clone(),
                variant: variant.clone(),
                binding: binding.clone(),
                outcome,
            });
        }
    }
    let complete = n.missing + n.blocked + n.pending == 0;
    let status = if n.failed + n.cancelled > 0 {
        "failed"
    } else if complete {
        "passed"
    } else {
        "incomplete"
    };
    let passed = status == "passed";
    let count = n.selected_cells - n.passed;
    let omitted = count - diagnostics.len();
    let results_sha256 = receipt.sha256();
    let json = bounded(
        format!(
            "{}\n",
            canonical(
                json!({"test-evaluation-version":1,"plan_digest":doc.plan_digest,"policy_sha256":p.policy_sha256,"bindings_sha256":bindings,"results_sha256":results_sha256,"source":p.source,"run_id":results.run_id,"attempt":results.attempt,"status":status,"complete":complete,"passed":passed,"exit_code":u8::from(!passed),"counts":n,"diagnostic_count":count,"omitted_diagnostics":omitted,"diagnostics":diagnostics})
            )
        ),
        "test result report",
    )?;
    let mut text = format!(
        "Test evaluation: {status}\nComplete: {}\nExecution commit: {}\nExecution tree: {}\nRun: {}; attempt: {}\nRequired cells: {}; received: {}; passed: {}\nDiagnostics: {count}\n",
        if complete { "yes" } else { "no" },
        p.source.execution_commit,
        p.source.execution_tree,
        results.run_id,
        results.attempt,
        n.selected_cells,
        n.received_cells,
        n.passed
    );
    for d in diagnostics {
        writeln!(
            text,
            "  [{}] {}/{}: {}",
            d.code, d.suite, d.variant, d.message
        )
        .unwrap();
    }
    if omitted > 0 {
        writeln!(
            text,
            "  {omitted} additional diagnostic(s) omitted from this display"
        )
        .unwrap();
    }
    text.push_str("Evaluation of supplied receipts only; no tests were executed.");
    Ok(Evaluation { passed, json, text })
}
