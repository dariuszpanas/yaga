//! Pure schema-v1 tracked-tree policy over an explicitly supplied selection.
use super::{
    DriveSyntax, PathComponents, PathError, Pattern, WorkBudget, sorted_selection, unique,
};

#[derive(Debug, Clone)]
pub struct TreePolicy {
    required: Vec<String>,
    forbidden: Vec<Pattern>,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Finding<'a> {
    pub code: &'static str,
    pub message: &'static str,
    pub path: &'a str,
    pub pattern: Option<&'a str>,
}

impl TreePolicy {
    pub fn required_paths(&self) -> &[String] {
        &self.required
    }
    pub fn forbidden_patterns(&self) -> impl Iterator<Item = &str> {
        self.forbidden.iter().map(Pattern::as_str)
    }
    /// Preserve the configured order and reject any required/forbidden overlap.
    pub fn new(required: &[&str], forbidden: &[&str]) -> Result<Self, PathError> {
        Self::with_drive_syntax(required, forbidden, DriveSyntax::NATIVE)
    }
    pub(super) fn with_drive_syntax(
        required: &[&str],
        forbidden: &[&str],
        syntax: DriveSyntax,
    ) -> Result<Self, PathError> {
        if !(1..=128).contains(&required.len().saturating_add(forbidden.len())) {
            return Err(PathError::Policy(
                "tree policy requires 1 through 128 combined path entries",
            ));
        }
        let forbidden_patterns = forbidden
            .iter()
            .map(|p| Pattern::compile_with_drive_syntax(p, syntax))
            .collect::<Result<Vec<_>, _>>()?;
        let mut work = WorkBudget::default();
        for path in required {
            let components = PathComponents::parse_with_drive_syntax(path, syntax)?;
            for pattern in &forbidden_patterns {
                if pattern.matches(&components, &mut work)? {
                    return Err(PathError::Configuration(format!(
                        "required path {} is forbidden by pattern {}",
                        crate::text::python_repr(path),
                        crate::text::python_repr(pattern.as_str())
                    )));
                }
            }
        }
        if !unique(required) || !unique(forbidden) {
            return Err(PathError::Policy("tree policy entries must be unique"));
        }
        Ok(Self {
            required: required.iter().map(|s| (*s).into()).collect(),
            forbidden: forbidden_patterns,
        })
    }

    /// Missing paths follow policy order, then forbidden paths follow tree order.
    pub fn check<'a>(&'a self, paths: &[&'a str]) -> Result<Vec<Finding<'a>>, PathError> {
        self.check_with_budget(paths, &mut WorkBudget::default())
    }

    /// Evaluate the selected paths against an explicit remaining work budget.
    pub fn check_with_budget<'a>(
        &'a self,
        paths: &[&'a str],
        work: &mut WorkBudget,
    ) -> Result<Vec<Finding<'a>>, PathError> {
        sorted_selection(paths, 50_000)?;
        let mut findings = Vec::new();
        for path in &self.required {
            if paths.binary_search(&path.as_str()).is_err() {
                findings.push(Finding {
                    code: "tree.required",
                    message: "required tracked path is missing",
                    path,
                    pattern: None,
                });
            }
        }
        for path in paths {
            let components = PathComponents::parse(path)?;
            for pattern in &self.forbidden {
                if pattern.matches(&components, work)? {
                    findings.push(Finding {
                        code: "tree.forbidden",
                        message: "tracked path matches a forbidden pattern",
                        path,
                        pattern: Some(pattern.as_str()),
                    });
                    break;
                }
            }
        }
        Ok(findings)
    }
}
