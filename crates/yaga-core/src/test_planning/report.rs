use super::*;
use serde_json::json;
use std::fmt::Write;

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Selective => "selective",
            Self::Full => "full",
        }
    }
}
pub fn policy_report(p: &TestPolicy, as_json: bool) -> String {
    let mut profiles: Vec<_> = p.profiles.iter().map(|(n, _)| n.as_str()).collect();
    profiles.sort_unstable();
    let cells: usize = p.suites.iter().map(|s| s.variants.len()).sum();
    if as_json {
        return canonical(
            json!({"schema_version":1,"kind":"test_policy","status":"valid","valid":true,"test-policy-version":1,"policy_sha256":p.sha256,"suites":p.suites.len(),"cells":cells,"profiles":profiles,"rules":p.rules.len()}),
        );
    }
    format!(
        "Test policy is valid (version 1).\nPolicy SHA-256: {}\nInventory: {} suite(s), {cells} variant cell(s)\nProfiles: {}\nRules: {}\nPolicy validation only; no tests were executed.",
        p.sha256,
        p.suites.len(),
        profiles.join(", "),
        p.rules.len()
    )
}
impl PlanDocument {
    pub fn text(&self) -> String {
        let p = &self.plan;
        let s = &p.selection;
        let mut out = format!(
            "Test plan: {}\nMode: {} requested; {} effective\nExecution commit: {}\nExecution tree: {}\nPolicy SHA-256: {}\nBindings SHA-256: {}\nPlan digest: {}\n",
            s.profile,
            s.requested_mode.as_str(),
            s.effective_mode.as_str(),
            p.source.execution_commit,
            p.source.execution_tree,
            p.policy_sha256,
            p.bindings_sha256
                .as_deref()
                .unwrap_or("none (preview only)"),
            self.plan_digest
        );
        if let Some(paths) = &s.changed_paths {
            writeln!(
                out,
                "Changed paths: {}\nComparison commit: {}",
                paths.len(),
                p.source.comparison_commit.as_deref().unwrap_or("")
            )
            .unwrap();
        } else {
            out.push_str("Changed paths: unavailable for a revision-only plan\n");
        }
        let cells: usize = s.selected.iter().map(|i| i.variants.len()).sum();
        writeln!(
            out,
            "Selected: {}/{} suite(s), {cells} variant cell(s)",
            s.selected.len(),
            s.inventory.len()
        )
        .unwrap();
        for i in &s.selected {
            writeln!(out, "  {}: {}", i.suite, i.variants.join(", ")).unwrap();
        }
        writeln!(out, "Reasons: {}", s.reasons.len()).unwrap();
        for r in s.reasons.iter().take(256) {
            write!(out, "  [{}]", r.code).unwrap();
            let mut sep = " ";
            for (key, value) in [
                ("suite", &r.suite),
                ("rule", &r.rule),
                ("path", &r.path),
                ("required_by", &r.required_by),
            ] {
                if let Some(v) = value {
                    write!(out, "{sep}{key}={}", safe_text(v, 200)).unwrap();
                    sep = "; ";
                }
            }
            if let Some(variants) = &r.variants {
                write!(out, "{sep}variants={}", variants.join(",")).unwrap();
            }
            out.push('\n');
        }
        if s.reasons.len() > 256 {
            writeln!(
                out,
                "  {} additional reason(s) omitted from this display",
                s.reasons.len() - 256
            )
            .unwrap();
        }
        out.push_str("Planning only; no tests were executed.");
        out
    }
}
