use super::*;
use crate::paths::{PathComponents, Pattern, WorkBudget};
use std::collections::{BTreeMap, BTreeSet, HashMap};

#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq, PartialOrd, Ord, Default)]
#[serde(deny_unknown_fields)]
pub struct Reason {
    pub code: String,
    pub suite: Option<String>,
    pub rule: Option<String>,
    pub path: Option<String>,
    pub required_by: Option<String>,
}
impl Reason {
    fn new(code: &str) -> Self {
        Self {
            code: code.into(),
            ..Self::default()
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SelectedSuite {
    pub suite: String,
    pub variants: Vec<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Selection {
    pub profile: String,
    pub requested_mode: Mode,
    pub effective_mode: Mode,
    pub changed_paths: Option<Vec<String>>,
    pub inventory: Vec<Suite>,
    pub selected: Vec<SelectedSuite>,
    pub reasons: Vec<Reason>,
}
struct Reasons {
    values: BTreeSet<Reason>,
    bytes: usize,
    limit: usize,
}
impl Reasons {
    fn add(&mut self, reason: Reason) -> Result<(), Error> {
        if self.values.contains(&reason) {
            return Ok(());
        }
        self.bytes += usize::from(!self.values.is_empty())
            + serde_json::to_vec(&reason)
                .expect("serializable reason")
                .len();
        if self.bytes > self.limit {
            return Err(Error::Configuration(format!(
                "test selection reasons exceed the hard {}-byte limit",
                self.limit
            )));
        }
        self.values.insert(reason);
        Ok(())
    }
}
struct Matches<'a> {
    paths: Vec<PathComponents<'a>>,
    work: WorkBudget,
    limit: usize,
    cache: HashMap<String, Vec<usize>>,
}
impl Matches<'_> {
    fn combined(&mut self, patterns: &[String]) -> Result<BTreeSet<usize>, Error> {
        let mut union = BTreeSet::new();
        let mut ordered: Vec<_> = patterns.iter().collect();
        ordered.sort_unstable();
        for p in ordered {
            if !self.cache.contains_key(p) {
                let pattern =
                    Pattern::compile(p).map_err(|e| Error::Configuration(e.to_string()))?;
                let mut matched = Vec::new();
                for (i, path) in self.paths.iter().enumerate() {
                    if pattern.matches(path, &mut self.work).map_err(|_| {
                        Error::Configuration(format!(
                            "test planning exceeds the hard {}-unit match-work limit",
                            self.limit
                        ))
                    })? {
                        matched.push(i);
                    }
                }
                self.cache.insert(p.clone(), matched);
            }
            union.extend(self.cache[p].iter().copied());
        }
        Ok(union)
    }
}
pub(super) fn checked_paths(
    raw: Option<&[String]>,
    drive_syntax: DriveSyntax,
) -> Result<Option<Vec<String>>, Error> {
    let Some(paths) = raw else { return Ok(None) };
    if paths.len() > 2048 {
        return Err(Error::Input(
            "changed paths must be an explicit tuple of at most 2048".into(),
        ));
    }
    let mut total = 0;
    for p in paths {
        PathComponents::parse_with_drive_syntax(p, drive_syntax)
            .map_err(|e| Error::Input(format!("invalid changed path for test planning: {e}")))?;
        total += p.len() + 1;
        if total > MAX_BYTES {
            return Err(Error::Input(format!(
                "changed paths exceed the hard {MAX_BYTES}-byte limit"
            )));
        }
    }
    let mut sorted = paths.to_vec();
    sorted.sort_unstable();
    if sorted.windows(2).any(|w| w[0] == w[1]) {
        return Err(Error::Input("changed paths must be unique".into()));
    }
    Ok(Some(sorted))
}

pub fn plan_tests(
    policy: &TestPolicy,
    name: &str,
    raw: Option<&[String]>,
) -> Result<Selection, Error> {
    plan_tests_with_limits(policy, name, raw, crate::paths::MAX_MATCH_WORK, MAX_BYTES)
}

/// Apply a consumer's narrower match-work and output budgets within the hard v1 limits.
pub fn plan_tests_with_limits(
    policy: &TestPolicy,
    name: &str,
    raw: Option<&[String]>,
    max_work: usize,
    max_bytes: usize,
) -> Result<Selection, Error> {
    plan_tests_with_path_repr(policy, name, raw, max_work, max_bytes, python_repr)
}

/// Use the calling interpreter's string representation for the bounded first
/// unknown path. Successful selections and the matching budget are independent of it.
pub fn plan_tests_with_path_repr(
    policy: &TestPolicy,
    name: &str,
    raw: Option<&[String]>,
    max_work: usize,
    max_bytes: usize,
    quote: impl FnOnce(&str) -> String,
) -> Result<Selection, Error> {
    let max_bytes = max_bytes.min(MAX_BYTES);
    policy.validate().map_err(Error::Configuration)?;
    let profile = resolve_profile(policy, name)?;
    let paths = checked_paths(raw, DriveSyntax::NATIVE)?;
    let mut reasons = Reasons {
        values: BTreeSet::new(),
        bytes: 2,
        limit: max_bytes,
    };
    let mut selected: BTreeSet<_> = profile.always.iter().cloned().collect();
    let mode = if profile.mode == Mode::Full {
        reasons.add(Reason::new("test.profile-full"))?;
        Mode::Full
    } else {
        let paths = paths.as_deref().ok_or_else(|| {
            Error::Input("selective test planning requires explicit changed paths".into())
        })?;
        for id in &selected {
            reasons.add(Reason {
                suite: Some(id.clone()),
                ..Reason::new("test.always")
            })?;
        }
        classify(policy, paths, &mut selected, &mut reasons, max_work, quote)?
    };
    let mut inventory = policy.suites.clone();
    inventory.sort_by(|a, b| a.id.cmp(&b.id));
    for s in &mut inventory {
        s.variants.sort();
        s.tags.sort();
        s.needs.sort();
        s.requires.sort();
    }
    let suites: BTreeMap<_, _> = inventory.iter().map(|s| (s.id.as_str(), s)).collect();
    if mode == Mode::Full {
        selected = suites.keys().map(|s| s.to_string()).collect();
    } else {
        let mut remaining = selected.clone();
        while let Some(id) = remaining.pop_first() {
            for need in &suites[&*id].needs {
                reasons.add(Reason {
                    suite: Some(need.clone()),
                    required_by: Some(id.clone()),
                    ..Reason::new("test.prerequisite")
                })?;
                if selected.insert(need.clone()) {
                    remaining.insert(need.clone());
                }
            }
        }
    }
    let mut ordered = Vec::new();
    while !selected.is_empty() {
        let ready = selected
            .iter()
            .find(|id| {
                suites[id.as_str()]
                    .needs
                    .iter()
                    .all(|n| !selected.contains(n))
            })
            .expect("validated acyclic inventory")
            .clone();
        selected.remove(&ready);
        ordered.push(SelectedSuite {
            variants: suites[ready.as_str()].variants.clone(),
            suite: ready,
        });
    }
    let result = Selection {
        profile: name.into(),
        requested_mode: profile.mode,
        effective_mode: mode,
        changed_paths: paths,
        inventory,
        selected: ordered,
        reasons: reasons.values.into_iter().collect(),
    };
    if serde_json::to_vec(&result)
        .expect("serializable selection")
        .len()
        > max_bytes
    {
        return Err(Error::Configuration(format!(
            "test selection exceeds the hard {max_bytes}-byte limit"
        )));
    }
    Ok(result)
}

fn classify(
    policy: &TestPolicy,
    paths: &[String],
    selected: &mut BTreeSet<String>,
    reasons: &mut Reasons,
    max_work: usize,
    quote: impl FnOnce(&str) -> String,
) -> Result<Mode, Error> {
    let components = paths
        .iter()
        .map(|p| PathComponents::parse(p).expect("validated path"))
        .collect();
    let work = WorkBudget::new(max_work);
    let mut matches = Matches {
        paths: components,
        limit: work.remaining(),
        work,
        cache: HashMap::new(),
    };
    let full = matches.combined(&policy.full_on)?;
    if !full.is_empty() {
        for i in full {
            reasons.add(Reason {
                path: Some(paths[i].clone()),
                ..Reason::new("test.full-trigger")
            })?;
        }
        return Ok(Mode::Full);
    }
    let mut unclassified: BTreeSet<_> = (0..paths.len()).collect();
    let mut rules: Vec<_> = policy.rules.iter().collect();
    rules.sort_by(|a, b| a.id.cmp(&b.id));
    for rule in rules {
        let matched = matches.combined(&rule.when_any)?;
        if matched.is_empty() {
            continue;
        }
        let mut targets: BTreeSet<_> = rule.select.iter().collect();
        targets.extend(
            policy
                .suites
                .iter()
                .filter(|s| s.tags.iter().any(|t| rule.select_tags.contains(t)))
                .map(|s| &s.id),
        );
        selected.extend(targets.iter().map(|s| (*s).clone()));
        for i in matched {
            unclassified.remove(&i);
            for id in &targets {
                reasons.add(Reason {
                    suite: Some((*id).clone()),
                    rule: Some(rule.id.clone()),
                    path: Some(paths[i].clone()),
                    ..Reason::new("test.rule")
                })?;
            }
        }
    }
    if unclassified.is_empty() {
        return Ok(Mode::Selective);
    }
    for i in matches.combined(&policy.no_impact)? {
        if unclassified.remove(&i) {
            reasons.add(Reason {
                path: Some(paths[i].clone()),
                ..Reason::new("test.no-impact")
            })?;
        }
    }
    if let Some(first) = unclassified.first() {
        if policy.unknown_paths == UnknownPaths::Error {
            return Err(Error::Configuration(format!(
                "test planning has {} unknown changed path(s); first: {}",
                unclassified.len(),
                quote(&safe_text(&paths[*first], 200))
            )));
        }
        for i in unclassified {
            reasons.add(Reason {
                path: Some(paths[i].clone()),
                ..Reason::new("test.unknown-path")
            })?;
        }
        return Ok(Mode::Full);
    }
    Ok(Mode::Selective)
}
