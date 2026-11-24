//! Evaluate the frozen trust rules on source-located facts from the shared composition.
use super::{
    Diagnostic,
    facts::{Facts, Field, Point, Step},
    yaml::Graph,
};
use crate::repository::SECURITY_RULES;
const MESSAGES: [&str; 7] = [
    "workflow must declare top-level permissions",
    "top-level write permission must be scoped to a job",
    "permissions must not grant write-all",
    "reusable workflow calls must enumerate inherited secrets",
    "privileged workflow must not check out an untrusted event ref",
    "actions/checkout must disable persisted credentials",
    "pull_request workflows must not grant pull-requests or statuses write access",
];
struct Check {
    enabled: [bool; 7],
    found: Vec<Diagnostic>,
}
impl Check {
    fn issue(&mut self, rule: usize, at: Point) -> Result<(), String> {
        if self.enabled[rule] {
            if self.found.len() == 512 {
                return Err(
                    "workflow security findings exceed the hard 512-diagnostic total limit".into(),
                );
            }
            self.found.push(Diagnostic {
                code: format!("security.{}", SECURITY_RULES[rule]),
                message: MESSAGES[rule].to_owned(),
                line: at.1,
                column: at.2,
            });
        }
        Ok(())
    }
    fn permissions(
        &mut self,
        facts: &Facts,
        field: &Field,
        top: bool,
        pr: bool,
    ) -> Result<(), String> {
        if field.0.text(facts) == Some("write-all") {
            self.issue(2, field.0)?
        }
        for (key, value) in &field.1 {
            let Some(name) = key.text(facts) else {
                continue;
            };
            if value.text(facts) != Some("write") {
                continue;
            }
            if top {
                self.issue(1, *value)?
            }
            if pr && matches!(name, "pull-requests" | "statuses") {
                self.issue(6, *value)?
            }
        }
        Ok(())
    }
    fn step(&mut self, facts: &Facts, step: &Step, privileged: bool) -> Result<(), String> {
        if !self.enabled[5] && (!privileged || !self.enabled[4]) {
            return Ok(());
        }
        let checkouts = step
            .uses
            .iter()
            .filter(|u| u.text(facts).is_some_and(is_checkout));
        if checkouts.clone().next().is_none() {
            return Ok(());
        }
        let mut unsafe_checkout = false;
        let mut unambiguous = step.inputs.len() == 1 && step.inputs[0].0.mapping();
        let mut persist_count = 0;
        for Field(_, pairs) in &step.inputs {
            for (key, value) in pairs {
                let Some((name, tag)) = key.scalar(facts) else {
                    unambiguous = false;
                    continue;
                };
                let folded = crate::text::casefold(name);
                if (folded == "allow-unsafe-pr-checkout" && value.text(facts) != Some("false"))
                    || (matches!(folded.as_str(), "ref" | "repository")
                        && value.text(facts).is_some_and(|s| s.contains("${{")))
                {
                    unsafe_checkout = true;
                }
                if !name.is_ascii() {
                    unambiguous = false
                }
                if name
                    .replace(' ', "_")
                    .eq_ignore_ascii_case("persist-credentials")
                {
                    persist_count += 1;
                    unambiguous &= tag == "tag:yaml.org,2002:str"
                        && matches!(
                            value.scalar(facts),
                            Some(("false", "tag:yaml.org,2002:str" | "tag:yaml.org,2002:bool"))
                        );
                }
            }
        }
        for uses in checkouts {
            if privileged && unsafe_checkout {
                self.issue(4, *uses)?
            }
            if !unambiguous || persist_count != 1 {
                self.issue(5, *uses)?
            }
        }
        Ok(())
    }
}

fn is_checkout(value: &str) -> bool {
    let Some((path, _)) = value.split_once('@') else {
        return false;
    };
    let mut parts = path.split(['/', '\\']).filter(|p| !p.is_empty());
    parts
        .next()
        .is_some_and(|p| p.eq_ignore_ascii_case("actions"))
        && parts
            .next()
            .is_some_and(|p| p.eq_ignore_ascii_case("checkout"))
}

pub(super) fn check(graph: &Graph, rules: &[String]) -> Result<Vec<Diagnostic>, String> {
    check_facts(&Facts::project(graph), rules)
}
pub(super) fn check_facts(facts: &Facts, rules: &[String]) -> Result<Vec<Diagnostic>, String> {
    if rules.is_empty() || rules.len() > SECURITY_RULES.len() {
        return Err("workflow security rules must be nonempty and unique".into());
    }
    let mut check = Check {
        enabled: [false; 7],
        found: Vec::new(),
    };
    for name in rules {
        let idx = SECURITY_RULES
            .iter()
            .position(|&r| r == name)
            .ok_or("workflow security rule is not recognized")?;
        if std::mem::replace(&mut check.enabled[idx], true) {
            return Err("workflow security rules must not be repeated".into());
        }
    }

    let event = |name: &str| {
        facts
            .triggers
            .iter()
            .flat_map(|(_, events)| events)
            .any(|e| e.text(facts) == Some(name))
    };
    let pr = event("pull_request");
    let privileged = event("pull_request_target") || event("workflow_run");
    if facts.permissions.is_empty() {
        check.issue(0, facts.root)?
    }
    for field in &facts.permissions {
        check.permissions(facts, field, true, pr)?
    }
    for job in &facts.jobs {
        for field in &job.permissions {
            check.permissions(facts, field, false, pr)?
        }
        if job.uses.iter().any(|u| u.scalar(facts).is_some()) {
            for value in &job.secrets {
                if value.text(facts) == Some("inherit") {
                    check.issue(3, *value)?
                }
            }
        }
        for step in &job.steps {
            check.step(facts, step, privileged)?
        }
    }
    check.found.sort();
    Ok(check.found)
}
