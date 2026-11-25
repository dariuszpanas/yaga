//! Lexical Windows compatibility rules for Git leaf names.
use super::{PathComponents, PathError, WorkBudget, leaf_topology, sorted_selection};
use std::collections::HashMap;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, serde::Serialize)]
pub enum Rule {
    #[serde(rename = "windows-characters")]
    Characters,
    #[serde(rename = "windows-trailing")]
    Trailing,
    #[serde(rename = "windows-reserved")]
    Reserved,
    #[serde(rename = "ascii-case-collision")]
    AsciiCaseCollision,
}

impl Rule {
    pub const ALL: [Self; 4] = [
        Self::Characters,
        Self::Trailing,
        Self::Reserved,
        Self::AsciiCaseCollision,
    ];
    pub fn name(self) -> &'static str {
        match self {
            Self::Characters => "windows-characters",
            Self::Trailing => "windows-trailing",
            Self::Reserved => "windows-reserved",
            Self::AsciiCaseCollision => "ascii-case-collision",
        }
    }
    pub fn code(self) -> &'static str {
        match self {
            Self::Characters => "path.windows-character",
            Self::Trailing => "path.windows-trailing",
            Self::Reserved => "path.windows-reserved",
            Self::AsciiCaseCollision => "path.ascii-case-collision",
        }
    }
}

#[derive(Debug)]
pub struct PathPolicy(Vec<Rule>, Option<&'static str>);

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Finding<'a> {
    pub code: &'static str,
    pub message: String,
    pub path: &'a str,
    pub component: Option<usize>,
    pub related_path: Option<&'a str>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Report<'a> {
    pub diagnostics: Vec<Finding<'a>>,
    pub rule_counts: Vec<(Rule, usize)>,
}

impl Report<'_> {
    pub fn finding_count(&self) -> usize {
        self.rule_counts.iter().map(|(_, n)| n).sum()
    }
}

#[derive(Default)]
struct Violations {
    character: Option<(usize, char)>,
    trailing: Option<usize>,
    reserved: Option<usize>,
}

fn reserved(component: &str) -> bool {
    let base = component.split('.').next().unwrap_or("");
    matches!(base, "con" | "prn" | "aux" | "nul")
        || base
            .strip_prefix("com")
            .or_else(|| base.strip_prefix("lpt"))
            .is_some_and(|n| {
                matches!(
                    n,
                    "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                )
            })
}

impl PathPolicy {
    pub fn new(rules: &[Rule]) -> Result<Self, PathError> {
        if !(1..=4).contains(&rules.len()) {
            return Err(PathError::Policy("expected 1 through 4 path rules"));
        }
        let mut rules = rules.to_vec();
        rules.sort_unstable();
        if rules.windows(2).any(|p| p[0] == p[1]) {
            return Err(PathError::Policy("path rules must be unique"));
        }
        Ok(Self(rules, None))
    }
    pub fn windows_compatible_v1() -> Self {
        Self(Rule::ALL.to_vec(), Some("windows-compatible-v1"))
    }
    pub fn rules(&self) -> &[Rule] {
        &self.0
    }
    pub fn profile(&self) -> Option<&'static str> {
        self.1
    }

    pub fn check<'a>(&self, paths: &[&'a str]) -> Result<Report<'a>, PathError> {
        self.check_with_budget(paths, &mut WorkBudget::default())
    }

    /// Evaluate the selected paths against an explicit remaining work budget.
    pub fn check_with_budget<'a>(
        &self,
        paths: &[&'a str],
        work: &mut WorkBudget,
    ) -> Result<Report<'a>, PathError> {
        sorted_selection(paths, 50_000)?;
        let mut local = Vec::with_capacity(paths.len());
        let mut folded_paths = Vec::new();
        let mut aliases = HashMap::<String, Vec<usize>>::new();
        let check_collision = self.0.contains(&Rule::AsciiCaseCollision);
        let check_reserved = self.0.contains(&Rule::Reserved);
        let check_characters = self.0.contains(&Rule::Characters);
        let check_trailing = self.0.contains(&Rule::Trailing);
        for (index, path) in paths.iter().enumerate() {
            let components = PathComponents::committed(path)?;
            work.spend(components.0.len())?;
            let mut violations = Violations::default();
            for (i, piece) in components.0.iter().enumerate() {
                let number = i + 1;
                if check_trailing && piece.text.ends_with([' ', '.']) {
                    violations.trailing.get_or_insert(number);
                }
                if check_characters || check_reserved || check_collision {
                    work.spend(piece.length)?;
                }
                if check_characters && violations.character.is_none() {
                    violations.character = piece
                        .text
                        .chars()
                        .find(|c| {
                            matches!(
                                c,
                                '<' | '>' | ':' | '"' | '\\' | '|' | '?' | '*' | '\u{1}'..='\u{1f}'
                            )
                        })
                        .map(|c| (number, c));
                }
                if check_reserved
                    && violations.reserved.is_none()
                    && reserved(&piece.text.to_ascii_lowercase())
                {
                    violations.reserved = Some(number);
                }
            }
            local.push(violations);
            if check_collision {
                let folded = path.to_ascii_lowercase();
                aliases.entry(folded.clone()).or_default().push(index);
                folded_paths.push(folded);
            }
        }
        leaf_topology(paths)?;
        let related = collisions(&folded_paths, &aliases, work)?;
        let mut report = Report {
            diagnostics: Vec::new(),
            rule_counts: self.0.iter().map(|&r| (r, 0)).collect(),
        };
        for (i, &path) in paths.iter().enumerate() {
            let local = &local[i];
            for (rule, count) in &mut report.rule_counts {
                let (component, related_path) = match rule {
                    Rule::Characters => (local.character.map(|(n, _)| n), None),
                    Rule::Trailing => (local.trailing, None),
                    Rule::Reserved => (local.reserved, None),
                    Rule::AsciiCaseCollision => (None, related[i].map(|j| paths[j])),
                };
                if component.is_none() && related_path.is_none() {
                    continue;
                }
                *count += 1;
                if report.diagnostics.len() == 256 {
                    continue;
                }
                let message = match rule {
                    Rule::Characters => format!(
                        "path component contains Windows-incompatible character U+{:04X}",
                        local.character.unwrap().1 as u32
                    ),
                    Rule::Trailing => "path component ends in an ASCII space or period".into(),
                    Rule::Reserved => {
                        "path component uses a reserved Windows device basename".into()
                    }
                    Rule::AsciiCaseCollision => {
                        "path has an ASCII case-insensitive checkout collision".into()
                    }
                };
                report.diagnostics.push(Finding {
                    code: rule.code(),
                    message,
                    path,
                    component,
                    related_path,
                });
            }
        }
        Ok(report)
    }
}

fn keep_first(current: &mut Option<usize>, candidate: usize) {
    *current = Some(current.map_or(candidate, |existing| existing.min(candidate)));
}

fn collisions(
    folded: &[String],
    aliases: &HashMap<String, Vec<usize>>,
    work: &mut WorkBudget,
) -> Result<Vec<Option<usize>>, PathError> {
    let mut related = vec![None; folded.len()];
    work.spend(folded.len())?;
    for group in aliases.values() {
        work.spend(group.len())?;
        if let [first, second, ..] = group.as_slice() {
            keep_first(&mut related[*first], *second);
            for &index in &group[1..] {
                keep_first(&mut related[index], *first);
            }
        }
    }
    let mut first_descendants = HashMap::new();
    for (index, path) in folded.iter().enumerate() {
        work.spend(path.chars().count())?;
        for (position, (offset, c)) in path.char_indices().enumerate() {
            if c == '/' {
                work.spend(position + 1)?;
                let prefix = &path[..offset];
                if let Some(group) = aliases.get(prefix) {
                    keep_first(&mut related[index], group[0]);
                    first_descendants.entry(prefix).or_insert(index);
                }
            }
        }
    }
    for (prefix, descendant) in first_descendants {
        let group = &aliases[prefix];
        work.spend(group.len())?;
        for &index in group {
            keep_first(&mut related[index], descendant);
        }
    }
    Ok(related)
}
