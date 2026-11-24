//! Schema-v1 changed-path coupling. Load and authenticate the Git range separately.
use super::{
    DriveSyntax, PathComponents, PathError, Pattern, WorkBudget, sorted_selection, unique,
};
use std::collections::{HashMap, HashSet};

/// A rule supplied in declaration order from the explicit policy.
#[derive(Debug)]
pub struct Rule<'a> {
    pub name: &'a str,
    pub when_any: &'a [&'a str],
    pub require_any: &'a [&'a str],
}

#[derive(Debug)]
struct CompiledRule {
    name: String,
    when_any: Vec<usize>,
    require_any: Vec<usize>,
}

#[derive(Debug)]
pub struct ChangePolicy {
    rules: Vec<CompiledRule>,
    patterns: Vec<Pattern>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Status {
    Skipped,
    Passed,
    Failed,
}

#[derive(Debug)]
pub struct RuleResult<'a> {
    pub name: &'a str,
    pub when_any: Vec<&'a str>,
    pub require_any: Vec<&'a str>,
    pub triggered_paths: Vec<&'a str>,
    pub required_paths: Vec<&'a str>,
}

impl RuleResult<'_> {
    pub fn status(&self) -> Status {
        if self.triggered_paths.is_empty() {
            Status::Skipped
        } else if self.required_paths.is_empty() {
            Status::Failed
        } else {
            Status::Passed
        }
    }
    pub const CODE: &'static str = "change.require_any";
}

pub(super) fn valid_name(name: &str) -> bool {
    (1..=64).contains(&name.len())
        && !name.starts_with('-')
        && !name.ends_with('-')
        && !name.contains("--")
        && name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}
impl ChangePolicy {
    /// Read back the normalized rules in their declared order.
    pub fn rules(&self) -> impl Iterator<Item = (&str, Vec<&str>, Vec<&str>)> {
        let names =
            |indexes: &[usize]| indexes.iter().map(|&i| self.patterns[i].as_str()).collect();
        self.rules.iter().map(move |rule| {
            (
                rule.name.as_str(),
                names(&rule.when_any),
                names(&rule.require_any),
            )
        })
    }
    pub fn new(rules: &[Rule<'_>]) -> Result<Self, PathError> {
        Self::with_drive_syntax(rules, DriveSyntax::NATIVE)
    }
    pub(super) fn with_drive_syntax(
        rules: &[Rule<'_>],
        syntax: DriveSyntax,
    ) -> Result<Self, PathError> {
        if !(1..=32).contains(&rules.len()) {
            return Err(PathError::Policy(
                "change policy requires 1 through 32 rules",
            ));
        }
        let mut names = HashSet::new();
        let mut compiled = Vec::new();
        let mut patterns = Vec::new();
        let mut indexes = HashMap::new();
        for rule in rules {
            let name = rule.name;
            if !valid_name(name) {
                return Err(PathError::Policy(
                    "change rule name must be a lowercase ASCII slug",
                ));
            }
            if !names.insert(name) {
                return Err(PathError::Policy("change policy rule names must be unique"));
            }
            compiled.push(CompiledRule {
                name: name.into(),
                when_any: compile_list(rule.when_any, syntax, &mut patterns, &mut indexes)?,
                require_any: compile_list(rule.require_any, syntax, &mut patterns, &mut indexes)?,
            });
        }
        Ok(Self {
            rules: compiled,
            patterns,
        })
    }

    pub fn check<'a>(&'a self, paths: &[&'a str]) -> Result<Vec<RuleResult<'a>>, PathError> {
        self.check_with_budget(paths, &mut WorkBudget::default())
    }

    /// Evaluate the selected paths against an explicit remaining work budget.
    pub fn check_with_budget<'a>(
        &'a self,
        paths: &[&'a str],
        work: &mut WorkBudget,
    ) -> Result<Vec<RuleResult<'a>>, PathError> {
        sorted_selection(paths, 2048)?;
        let components = paths
            .iter()
            .map(|p| PathComponents::parse(p))
            .collect::<Result<Vec<_>, _>>()?;
        let mut matches = vec![None; self.patterns.len()];
        // Each distinct pattern is evaluated only when a rule first needs it.
        let mut combined = |indexes: &[usize]| -> Result<Vec<&'a str>, PathError> {
            let mut union = vec![false; paths.len()];
            for &index in indexes {
                if matches[index].is_none() {
                    let found = components
                        .iter()
                        .map(|p| self.patterns[index].matches(p, work))
                        .collect::<Result<Vec<_>, _>>()?;
                    matches[index] = Some(found);
                }
                for (selected, matched) in union.iter_mut().zip(matches[index].as_ref().unwrap()) {
                    *selected |= matched;
                }
            }
            Ok(paths
                .iter()
                .zip(union)
                .filter_map(|(&p, matched)| matched.then_some(p))
                .collect())
        };
        let mut results = Vec::new();
        for rule in &self.rules {
            let triggered_paths = combined(&rule.when_any)?;
            let required_paths = if triggered_paths.is_empty() {
                Vec::new()
            } else {
                combined(&rule.require_any)?
            };
            results.push(RuleResult {
                name: &rule.name,
                when_any: rule
                    .when_any
                    .iter()
                    .map(|&i| self.patterns[i].as_str())
                    .collect(),
                require_any: rule
                    .require_any
                    .iter()
                    .map(|&i| self.patterns[i].as_str())
                    .collect(),
                triggered_paths,
                required_paths,
            });
        }
        Ok(results)
    }
}

fn compile_list<'a>(
    list: &[&'a str],
    syntax: DriveSyntax,
    patterns: &mut Vec<Pattern>,
    indexes: &mut HashMap<&'a str, usize>,
) -> Result<Vec<usize>, PathError> {
    if !(1..=16).contains(&list.len()) {
        return Err(PathError::Policy(
            "change rule requires 1 through 16 patterns per selector",
        ));
    }
    let mut ids = Vec::new();
    for &source in list {
        if source.len() > super::MAX_PATTERN_BYTES {
            return Err(PathError::InvalidPattern);
        }
        let index = match indexes.entry(source) {
            std::collections::hash_map::Entry::Occupied(entry) => *entry.get(),
            std::collections::hash_map::Entry::Vacant(entry) => {
                let id = patterns.len();
                patterns.push(Pattern::compile_with_drive_syntax(source, syntax)?);
                *entry.insert(id)
            }
        };
        ids.push(index);
    }
    if !unique(list) {
        return Err(PathError::Policy(
            "change rule selector patterns must be unique",
        ));
    }
    Ok(ids)
}
