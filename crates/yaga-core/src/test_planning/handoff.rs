//! Check a complete, fixed executor registry before handing suites to a consumer.
use super::*;
use crate::policies::testing::{token, tokens};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Executor {
    pub suite: String,
    pub binding: String,
    pub dispatch: String,
    pub variants: Vec<String>,
    pub capabilities: Vec<String>,
    pub definition_sha256: String,
}
pub struct Registry {
    executors: BTreeMap<String, Executor>,
    sha256: String,
}
impl Registry {
    pub fn new(mut entries: Vec<Executor>) -> Result<Self, Error> {
        if !(1..=128).contains(&entries.len()) {
            return Err(Error::Configuration(
                "executor registry requires a bounded nonempty immutable tuple".into(),
            ));
        }
        let mut ids = BTreeSet::new();
        let mut dispatch = BTreeSet::new();
        let mut capabilities = BTreeSet::new();
        let mut cells = 0;
        for e in &mut entries {
            for (label, s) in [
                ("suite id", &e.suite),
                ("suite binding", &e.binding),
                ("executor dispatch", &e.dispatch),
            ] {
                token(s, label).map_err(Error::Configuration)?;
            }
            tokens(&e.variants, "suite variants", 1, 16).map_err(Error::Configuration)?;
            tokens(&e.capabilities, "suite requires", 0, 16).map_err(Error::Configuration)?;
            if !is_digest(&e.definition_sha256) {
                return Err(Error::Configuration(
                    "executor definition_sha256 must be a lowercase SHA-256 digest".into(),
                ));
            }
            if !ids.insert(e.suite.clone()) {
                return Err(Error::Configuration(
                    "executor registry requires unique suite identifiers".into(),
                ));
            }
            if !dispatch.insert(e.dispatch.clone()) {
                return Err(Error::Configuration(
                    "executor registry requires unique dispatch identifiers".into(),
                ));
            }
            e.capabilities.sort();
            e.variants.sort();
            cells += e.variants.len();
            capabilities.extend(e.capabilities.iter().cloned());
        }
        if cells > 512 {
            return Err(Error::Configuration(
                "executor registry exceeds the expanded variant cell limit".into(),
            ));
        }
        if capabilities.len() > 128 {
            return Err(Error::Configuration(
                "executor registry exceeds the distinct capability limit".into(),
            ));
        }
        let mut registry = Self {
            executors: entries.into_iter().map(|e| (e.suite.clone(), e)).collect(),
            sha256: String::new(),
        };
        registry.sha256 = format!(
            "{:x}",
            Sha256::digest(bounded(canonical(registry.document()), "test handoff")?.as_bytes())
        );
        Ok(registry)
    }
    pub fn sha256(&self) -> &str {
        &self.sha256
    }
    pub fn document(&self) -> Value {
        json!({"test-executor-registry-version":1,"executors":self.executors.values().collect::<Vec<_>>()})
    }
}
pub fn create_handoff(
    doc: &PlanDocument,
    registry: &Registry,
    policy_sha256: &str,
    execution_commit: &str,
    execution_tree: &str,
) -> Result<Value, Error> {
    if !is_digest(policy_sha256) {
        return Err(Error::Input(
            "expected policy identity must be a lowercase SHA-256 digest".into(),
        ));
    }
    if !decode::valid_oid(execution_commit)
        || !decode::valid_oid(execution_tree)
        || execution_commit.len() != execution_tree.len()
    {
        return Err(Error::Input(
            "expected execution identities must be same-format Git object IDs".into(),
        ));
    }
    doc.validate()?;
    let p = &doc.plan;
    if p.policy_sha256 != policy_sha256 {
        return Err(Error::Input(
            "test handoff policy identity does not match the expected policy".into(),
        ));
    }
    if p.source.execution_commit != execution_commit || p.source.execution_tree != execution_tree {
        return Err(Error::Input(
            "test handoff source does not match the expected execution identity".into(),
        ));
    }
    let Some(bindings) = &p.bindings_sha256 else {
        return Err(Error::Input(
            "preview test plans without a bindings identity cannot be handed off".into(),
        ));
    };
    if bindings != registry.sha256() {
        return Err(Error::Input(
            "test handoff binding identity does not match the executor registry".into(),
        ));
    }
    let entries = &registry.executors;
    let suites: BTreeMap<_, _> = p.selection.inventory.iter().map(|s| (&s.id, s)).collect();
    if entries.len() != suites.len() || suites.keys().any(|s| !entries.contains_key(*s)) {
        return Err(Error::Configuration(
            "executor registry must map the complete catalog exactly once".into(),
        ));
    }
    for suite in &p.selection.inventory {
        let e = &entries[&suite.id];
        if e.binding != suite.binding {
            return Err(Error::Configuration(
                "executor binding does not match the catalog binding".into(),
            ));
        }
        if e.variants != suite.variants {
            return Err(Error::Configuration(
                "executor must preserve every catalog variant exactly".into(),
            ));
        }
        if suite.requires.iter().any(|c| !e.capabilities.contains(c)) {
            return Err(Error::Configuration(
                "executor is missing a required catalog capability".into(),
            ));
        }
    }
    let selected_ids: BTreeSet<_> = p.selection.selected.iter().map(|s| &s.suite).collect();
    let dispatch: BTreeMap<_, _> = entries
        .values()
        .map(|e| (&e.dispatch, selected_ids.contains(&e.suite)))
        .collect();
    let mut selected = Vec::new();
    let mut cells = Vec::new();
    for item in &p.selection.selected {
        let e = &entries[&item.suite];
        let needs: Vec<_> = suites[&item.suite]
            .needs
            .iter()
            .map(|s| &entries[s].dispatch)
            .collect();
        selected.push(json!({"suite":item.suite,"binding":e.binding,"dispatch":e.dispatch,"variants":item.variants,"needs":needs}));
        for v in &item.variants {
            cells.push(
                json!({"suite":item.suite,"variant":v,"binding":e.binding,"dispatch":e.dispatch}),
            );
        }
    }
    let out = json!({"test-handoff-version":1,"plan_digest":doc.plan_digest,"policy_sha256":p.policy_sha256,"bindings_sha256":bindings,"source":p.source,"profile":p.selection.profile,"requested_mode":p.selection.requested_mode,"effective_mode":p.selection.effective_mode,"dispatch":dispatch,"selected":selected,"cells":cells});
    bounded(canonical(out.clone()), "test handoff")?;
    Ok(out)
}
