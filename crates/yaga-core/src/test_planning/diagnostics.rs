//! Summaries of recorded selection causes. No filesystem, Git, or pattern matching.
use super::*;
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};
pub fn diagnose(doc: &PlanDocument, baseline: Option<&[String]>) -> Result<Value, Error> {
    doc.validate()?;
    let p = &doc.plan;
    let s = &p.selection;
    let catalog: BTreeSet<_> = s.inventory.iter().map(|x| x.id.as_str()).collect();
    let selected: BTreeSet<_> = s.selected.iter().map(|x| x.suite.as_str()).collect();
    let comparison = if let Some(ids) = baseline {
        let supplied: BTreeSet<_> = ids.iter().map(|s| s.as_str()).collect();
        if ids.len() > 128 || supplied.len() != ids.len() || !supplied.is_subset(&catalog) {
            return Err(Error::Input(
                "baseline suites must be a bounded tuple of unique catalog suite IDs".into(),
            ));
        }
        Some(
            json!({"baseline":supplied,"selected":selected,"additional":selected.difference(&supplied).collect::<Vec<_>>(),"missing":supplied.difference(&selected).collect::<Vec<_>>()}),
        )
    } else {
        None
    };
    let mut by_code: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
    let mut by_rule: BTreeMap<&str, (BTreeSet<&str>, BTreeSet<&str>)> = BTreeMap::new();
    let mut classified = BTreeSet::new();
    for reason in &s.reasons {
        if let Some(path) = reason.path.as_deref() {
            classified.insert(path);
            by_code.entry(&reason.code).or_default().insert(path);
            if let (Some(rule), Some(suite)) = (reason.rule.as_deref(), reason.suite.as_deref()) {
                let (paths, suites) = by_rule.entry(rule).or_default();
                paths.insert(path);
                suites.insert(suite);
            }
        }
    }
    let rules:Vec<_>=by_rule.into_iter().map(|(rule,(paths,suites))|json!({"rule":rule,"paths":paths,"suites":suites,"path_count":paths.len(),"suite_count":suites.len()})).collect();
    let fallback = ["profile-full", "full-trigger", "unknown-path"]
        .into_iter()
        .find(|name| {
            s.reasons
                .iter()
                .any(|r| r.code.strip_prefix("test.") == Some(name))
        })
        .unwrap_or("selective");
    let catalog_cells: usize = s.inventory.iter().map(|i| i.variants.len()).sum();
    let selected_cells: usize = s.selected.iter().map(|i| i.variants.len()).sum();
    let breadth = if selected.is_empty() {
        "empty"
    } else if selected == catalog && selected_cells == catalog_cells {
        "all"
    } else {
        "partial"
    };
    let paths_for = |code: &str| {
        s.changed_paths
            .as_ref()
            .map(|_| by_code.get(code).cloned().unwrap_or_default())
    };
    let unclassified = s.changed_paths.as_ref().map(|v| {
        v.iter()
            .filter(|p| !classified.contains(p.as_str()))
            .collect::<Vec<_>>()
    });
    let doc = json!({"test-selection-diagnostics-version":1,"plan_digest":doc.plan_digest,"policy_sha256":p.policy_sha256,"bindings_sha256":p.bindings_sha256,"source":p.source,"profile":s.profile,"requested_mode":s.requested_mode,"effective_mode":s.effective_mode,"selection_breadth":breadth,"fallback_cause":fallback,"counts":{"catalog_suites":catalog.len(),"selected_suites":selected.len(),"catalog_cells":catalog_cells,"selected_cells":selected_cells,"changed_paths":s.changed_paths.as_ref().map(Vec::len)},"full_trigger_paths":paths_for("test.full-trigger"),"unknown_paths":paths_for("test.unknown-path"),"no_impact_paths":paths_for("test.no-impact"),"paths_without_recorded_classification":unclassified,"rules":rules,"comparison":comparison});
    bounded(format!("{}\n", canonical(doc.clone())), "test diagnostics")?;
    Ok(doc)
}
