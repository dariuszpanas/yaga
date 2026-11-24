//! First-match Git-mode policy over validated tree records.
use super::records::{ModeKind, ModeSelection};
use super::{DriveSyntax, Overrides, PathComponents, PathError, WorkBudget};

#[derive(Debug)]
pub struct ModePolicy {
    default: Vec<ModeKind>,
    overrides: Overrides<Vec<ModeKind>>,
}

fn allowed(modes: &[ModeKind]) -> Result<Vec<ModeKind>, PathError> {
    if !(1..=4).contains(&modes.len()) {
        return Err(PathError::Policy("expected 1 through 4 allowed modes"));
    }
    let mut modes = modes.to_vec();
    modes.sort_unstable();
    if modes.windows(2).any(|m| m[0] == m[1]) {
        return Err(PathError::Policy("allowed modes must be unique"));
    }
    Ok(modes)
}

#[derive(Debug)]
pub struct Decision<'a> {
    pub path: &'a str,
    pub allowed_modes: &'a [ModeKind],
    pub pattern: Option<&'a str>,
}

#[derive(Debug)]
pub struct Finding<'a> {
    pub code: &'static str,
    pub message: &'static str,
    pub path: &'a str,
    pub actual_kind: ModeKind,
    pub actual_mode: &'static str,
    pub allowed_modes: &'a [ModeKind],
    pub pattern: Option<&'a str>,
}

#[derive(Debug)]
pub struct Report<'a> {
    pub finding_count: usize,
    pub diagnostics: Vec<Finding<'a>>,
    pub decisions: Vec<Decision<'a>>,
}

impl ModePolicy {
    pub fn default_allowed_modes(&self) -> &[ModeKind] {
        &self.default
    }
    pub fn path_overrides(&self) -> impl Iterator<Item = (&str, &[ModeKind])> {
        self.overrides
            .0
            .iter()
            .map(|(p, v)| (p.as_str(), v.as_slice()))
    }
    pub fn new(default: &[ModeKind], overrides: &[(&str, &[ModeKind])]) -> Result<Self, PathError> {
        Self::with_drive_syntax(default, overrides, DriveSyntax::NATIVE)
    }
    pub(super) fn with_drive_syntax(
        default: &[ModeKind],
        overrides: &[(&str, &[ModeKind])],
        syntax: DriveSyntax,
    ) -> Result<Self, PathError> {
        Ok(Self {
            default: allowed(default)?,
            overrides: Overrides::new(overrides, syntax, |v| allowed(v))?,
        })
    }

    pub fn check<'a>(&'a self, selection: &ModeSelection<'a>) -> Result<Report<'a>, PathError> {
        let entries: Vec<_> = selection
            .entries()
            .iter()
            .map(|entry| (entry.path, entry.kind))
            .collect();
        self.check_entries(&entries, &mut WorkBudget::default())
    }

    /// Evaluate canonical paths and kinds from an already collected, sorted leaf selection.
    pub fn check_entries<'a>(
        &'a self,
        entries: &[(&'a str, ModeKind)],
        work: &mut WorkBudget,
    ) -> Result<Report<'a>, PathError> {
        let paths: Vec<_> = entries.iter().map(|(path, _)| *path).collect();
        super::sorted_selection(&paths, 50_000)?;
        super::leaf_topology(&paths)?;
        let mut report = Report {
            finding_count: 0,
            diagnostics: Vec::new(),
            decisions: Vec::new(),
        };
        for &(path, kind) in entries {
            let components = PathComponents::committed(path)?;
            let (pattern, allowed_modes) =
                self.overrides.select(&components, &self.default, work)?;
            report.decisions.push(Decision {
                path,
                allowed_modes,
                pattern,
            });
            if !allowed_modes.contains(&kind) {
                report.finding_count += 1;
                if report.diagnostics.len() < 256 {
                    report.diagnostics.push(Finding {
                        code: "mode.disallowed",
                        message: "committed entry mode is not allowed by policy",
                        path,
                        actual_kind: kind,
                        actual_mode: kind.mode(),
                        allowed_modes,
                        pattern,
                    });
                }
            }
        }
        Ok(report)
    }
}
