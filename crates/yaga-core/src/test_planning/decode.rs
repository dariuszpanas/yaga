use super::*;
use crate::policies::testing::token;
use std::collections::{BTreeMap, BTreeSet};

pub fn load_json(path: &Path) -> Result<Value, Error> {
    let bytes = soft_canonicalize::soft_canonicalize(path)
        .and_then(|p| read_regular_prefix(&p, MAX_BYTES))
        .map_err(|_| Error::Input("cannot read a regular test evaluation input file".into()))?;
    if bytes.len() > MAX_BYTES {
        return Err(Error::Input(format!(
            "test evaluation input exceeds the hard {MAX_BYTES}-byte limit"
        )));
    }
    let invalid = || Error::Input("test evaluation input must be bounded UTF-8 JSON".into());
    let (mut depth, mut quoted, mut escape, mut numeric) = (0usize, false, false, 0usize);
    for &b in &bytes {
        if quoted {
            if escape {
                escape = false;
            } else if b == b'\\' {
                escape = true;
            } else if b == b'"' {
                quoted = false;
            }
        } else {
            match b {
                b'"' => quoted = true,
                b'{' | b'[' => {
                    depth += 1;
                    if depth > 16 {
                        return Err(invalid());
                    }
                }
                b'}' | b']' => depth = depth.checked_sub(1).ok_or_else(invalid)?,
                _ => (),
            }
            if b.is_ascii_digit() || matches!(b, b'-' | b'+' | b'.' | b'e' | b'E') {
                numeric += 1;
                if numeric > 64 {
                    return Err(invalid());
                }
            } else {
                numeric = 0;
            }
        }
    }
    crate::json::strict_value(&bytes).map_err(|_| invalid())
}
pub(super) fn valid_oid(s: &str) -> bool {
    matches!(s.len(), 40 | 64)
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn exact_fields(v: &Value, fields: &[&str]) -> bool {
    v.as_object()
        .is_some_and(|o| o.len() == fields.len() && fields.iter().all(|&f| o.contains_key(f)))
}
pub fn parse_plan(value: Value) -> Result<PlanDocument, Error> {
    parse_plan_with_drive_syntax(value, DriveSyntax::NATIVE)
}

/// Validate a plan using the same drive grammar as the client that created it.
pub fn parse_plan_with_drive_syntax(
    value: Value,
    drive_syntax: DriveSyntax,
) -> Result<PlanDocument, Error> {
    let fields_error =
        || Error::Input("test document requires exactly its versioned object fields".into());
    let semantics = || Error::Input("invalid version-one test plan semantics".into());
    if !exact_fields(&value, &["test-plan-version", "plan_digest", "plan"]) {
        return Err(fields_error());
    }
    let version_error = || Error::Input("unsupported test document version".into());
    if value["test-plan-version"].as_u64() != Some(1) {
        return Err(version_error());
    }
    if !value["plan_digest"]
        .as_str()
        .is_some_and(|digest| digest.len() <= 64 || digest.chars().nth(64).is_none())
    {
        return Err(Error::Input(
            "test document string has an invalid type or exceeds its length limit".into(),
        ));
    }
    let body = &value["plan"];
    if !exact_fields(
        body,
        &[
            "test-plan-version",
            "policy_sha256",
            "bindings_sha256",
            "profile",
            "requested_mode",
            "effective_mode",
            "source",
            "changed_paths",
            "inventory",
            "selected",
            "reasons",
        ],
    ) {
        return Err(fields_error());
    }
    if body["test-plan-version"].as_u64() != Some(1) {
        return Err(version_error());
    }
    fn bounded_structure(value: &Value, depth: usize, remaining: &mut usize) -> Result<(), Error> {
        let invalid = || Error::Input("test document exceeds the JSON structure limit".into());
        *remaining = remaining.checked_sub(1).ok_or_else(invalid)?;
        if depth > 8 {
            return Err(invalid());
        }
        match value {
            Value::Array(v) => {
                for x in v {
                    bounded_structure(x, depth + 1, remaining)?;
                }
            }
            Value::Object(v) => {
                for x in v.values() {
                    *remaining = remaining.checked_sub(1).ok_or_else(invalid)?;
                    bounded_structure(x, depth + 1, remaining)?;
                }
            }
            Value::Number(n) if !n.as_u64().is_some_and(|s| s <= MAX_BYTES as u64) => {
                return Err(Error::Input(
                    "test document integer is outside its bounded range".into(),
                ));
            }
            _ => (),
        }
        Ok(())
    }
    let mut remaining = MAX_BYTES;
    bounded_structure(&value, 0, &mut remaining)?;
    if canonical(value.clone()).len() >= MAX_BYTES {
        return Err(Error::Configuration(format!(
            "test diagnostics exceed the hard {MAX_BYTES}-byte limit"
        )));
    }
    let mut plan: PlanDocument = serde_json::from_value(value.clone()).map_err(|_| semantics())?;
    // Derive models share optional/default serde fields with TOML. Exact re-encoding
    // rejects omitted or extra keys throughout the transport before semantic checks.
    if serde_json::to_value(&plan).expect("serializable plan") != value {
        return Err(fields_error());
    }
    plan.drive_syntax = drive_syntax;
    plan.validate()?;
    Ok(plan)
}
impl PlanDocument {
    pub(super) fn validate(&self) -> Result<(), Error> {
        if self.version != 1 {
            return Err(Error::Input(
                "invalid version-one test plan semantics".into(),
            ));
        }
        self.plan.validate_semantics(self.drive_syntax)?;
        if self.plan_digest != self.plan.digest()? {
            return Err(Error::Input(
                "test plan document does not match its canonical semantics and digest".into(),
            ));
        }
        Ok(())
    }
}

impl Plan {
    pub(super) fn validate_semantics(&self, drive_syntax: DriveSyntax) -> Result<(), Error> {
        let bad = || Error::Input("invalid version-one test plan semantics".into());
        let p = self;
        let src = &p.source;
        let selection = &p.selection;
        let range = src.head_commit.is_some();
        let ids = [
            Some(&src.execution_commit),
            Some(&src.execution_tree),
            src.head_commit.as_ref(),
            src.base_commit.as_ref(),
            src.comparison_commit.as_ref(),
        ];
        if p.version != 1
            || !is_digest(&p.policy_sha256)
            || p.bindings_sha256.as_deref().is_some_and(|s| !is_digest(s))
            || ids
                .into_iter()
                .flatten()
                .any(|o| !valid_oid(o) || o.len() != src.execution_commit.len())
            || range != src.base_commit.is_some()
            || range != src.comparison_commit.is_some()
            || range != selection.changed_paths.is_some()
            || src
                .head_commit
                .as_ref()
                .is_some_and(|h| h != &src.execution_commit)
            || (!range && selection.requested_mode != Mode::Full)
        {
            return Err(bad());
        }
        if selection::checked_paths(selection.changed_paths.as_deref(), drive_syntax)
            .map_err(|_| bad())?
            != selection.changed_paths
        {
            return Err(bad());
        }
        let catalog = TestPolicy {
            suites: selection.inventory.clone(),
            profiles: vec![(
                selection.profile.clone(),
                Profile {
                    mode: selection.requested_mode,
                    always: vec![],
                },
            )],
            rules: vec![],
            full_on: vec![],
            no_impact: vec![],
            unknown_paths: UnknownPaths::Full,
            sha256: p.policy_sha256.clone(),
        };
        catalog.validate().map_err(|_| bad())?;
        if selection.inventory.windows(2).any(|w| w[0].id >= w[1].id)
            || selection.inventory.iter().any(|s| {
                [&s.tags, &s.needs, &s.variants, &s.requires]
                    .into_iter()
                    .any(|v| !v.is_sorted())
            })
        {
            return Err(bad());
        }
        let suites: BTreeMap<_, _> = selection
            .inventory
            .iter()
            .map(|s| (s.id.as_str(), s))
            .collect();
        let selected: BTreeSet<_> = selection
            .selected
            .iter()
            .map(|s| s.suite.as_str())
            .collect();
        if selected.len() != selection.selected.len()
            || selection.selected.iter().any(|s| {
                suites
                    .get(s.suite.as_str())
                    .is_none_or(|t| s.variants != t.variants)
            })
            || (selection.effective_mode == Mode::Full && selected.len() != suites.len())
        {
            return Err(bad());
        }
        let mut visited = BTreeSet::new();
        let mut remaining = selected.clone();
        for item in &selection.selected {
            let ready = remaining.iter().find(|&&n| {
                suites[n]
                    .needs
                    .iter()
                    .all(|need| visited.contains(need.as_str()))
            });
            if ready.copied() != Some(item.suite.as_str()) {
                return Err(bad());
            }
            remaining.remove(item.suite.as_str());
            visited.insert(item.suite.as_str());
        }
        if selection.reasons.len() > MAX_BYTES / 64
            || selection.reasons.windows(2).any(|w| w[0] >= w[1])
        {
            return Err(bad());
        }
        let mut fallback = false;
        for r in &selection.reasons {
            let fields = [
                r.suite.is_some(),
                r.rule.is_some(),
                r.path.is_some(),
                r.required_by.is_some(),
            ];
            let expected = match r.code.as_str() {
                "test.always" => [true, false, false, false],
                "test.rule" => [true, true, true, false],
                "test.profile-full" if selection.requested_mode == Mode::Full => [false; 4],
                "test.full-trigger" | "test.unknown-path" => {
                    fallback = true;
                    [false, false, true, false]
                }
                "test.no-impact" => [false, false, true, false],
                "test.prerequisite" => [true, false, false, true],
                _ => return Err(bad()),
            };
            if fields != expected
                || r.suite.as_deref().is_some_and(|s| !selected.contains(s))
                || r.rule.as_deref().is_some_and(|s| token(s, "rule").is_err())
                || r.path.as_ref().is_some_and(|s| {
                    selection
                        .changed_paths
                        .as_ref()
                        .is_none_or(|v| v.binary_search(s).is_err())
                })
                || r.required_by.as_deref().is_some_and(|s| {
                    !selected.contains(s)
                        || r.suite
                            .as_ref()
                            .is_none_or(|n| !suites[s].needs.contains(n))
                })
            {
                return Err(bad());
            }
        }
        if selection.requested_mode == Mode::Full {
            if selection.effective_mode != Mode::Full
                || selection.reasons.len() != 1
                || selection.reasons[0].code != "test.profile-full"
            {
                return Err(bad());
            }
        } else if fallback != (selection.effective_mode == Mode::Full) {
            return Err(bad());
        }
        Ok(())
    }
}
