//! Per-blob and aggregate size policy for stored objects in a committed Git tree.
use super::records::{MAX_BLOB_BYTES, SizeSelection};
use super::{DriveSyntax, Overrides, PathComponents, PathError, WorkBudget};

#[derive(Debug)]
pub struct SizePolicy {
    default: u64,
    total: Option<u64>,
    overrides: Overrides<u64>,
}

fn portable_size(value: u64) -> Result<u64, PathError> {
    if value > MAX_BLOB_BYTES {
        return Err(PathError::Policy(
            "configured byte limit exceeds the portable-integer limit",
        ));
    }
    Ok(value)
}

#[derive(Debug)]
pub struct Decision<'a> {
    pub path: &'a str,
    pub max_blob_bytes: u64,
    pub pattern: Option<&'a str>,
}

#[derive(Debug)]
pub struct Finding<'a> {
    pub code: &'static str,
    pub message: &'static str,
    pub path: Option<&'a str>,
    pub size: u64,
    pub limit: u64,
    pub pattern: Option<&'a str>,
}

#[derive(Debug)]
pub struct Report<'a> {
    pub total_bytes: u64,
    pub diagnostics: Vec<Finding<'a>>,
    pub decisions: Vec<Decision<'a>>,
}

impl SizePolicy {
    pub fn default_max_blob_bytes(&self) -> u64 {
        self.default
    }
    pub fn max_total_blob_bytes(&self) -> Option<u64> {
        self.total
    }
    pub fn path_limits(&self) -> impl Iterator<Item = (&str, u64)> {
        self.overrides.0.iter().map(|(p, v)| (p.as_str(), *v))
    }
    pub fn new(
        default: u64,
        total: Option<u64>,
        overrides: &[(&str, u64)],
    ) -> Result<Self, PathError> {
        Self::with_drive_syntax(default, total, overrides, DriveSyntax::NATIVE)
    }
    pub(super) fn with_drive_syntax(
        default: u64,
        total: Option<u64>,
        overrides: &[(&str, u64)],
        syntax: DriveSyntax,
    ) -> Result<Self, PathError> {
        Ok(Self {
            default: portable_size(default)?,
            total: total.map(portable_size).transpose()?,
            overrides: Overrides::new(overrides, syntax, |&n| portable_size(n))?,
        })
    }

    pub fn check<'a>(&'a self, selection: &SizeSelection<'a>) -> Result<Report<'a>, PathError> {
        let blobs: Vec<_> = selection
            .blobs()
            .iter()
            .map(|(entry, size)| (entry.path, *size))
            .collect();
        self.check_blobs(&blobs, &mut WorkBudget::default())
    }

    /// Evaluate per-path sizes from an already collected, sorted blob selection.
    pub fn check_blobs<'a>(
        &'a self,
        blobs: &[(&'a str, u64)],
        work: &mut WorkBudget,
    ) -> Result<Report<'a>, PathError> {
        let paths: Vec<_> = blobs.iter().map(|(path, _)| *path).collect();
        super::sorted_selection(&paths, 50_000)?;
        super::leaf_topology(&paths)?;
        let mut total_bytes = 0;
        for (_, size) in blobs {
            if *size > MAX_BLOB_BYTES - total_bytes {
                return Err(PathError::Selection(
                    "committed blob total exceeds the portable-integer limit",
                ));
            }
            total_bytes += size;
        }
        let mut report = Report {
            total_bytes,
            diagnostics: Vec::new(),
            decisions: Vec::new(),
        };
        for &(path, size) in blobs {
            let components = PathComponents::parse(path)?;
            let (pattern, &limit) = self.overrides.select(&components, &self.default, work)?;
            report.decisions.push(Decision {
                path,
                max_blob_bytes: limit,
                pattern,
            });
            if size > limit {
                report.diagnostics.push(Finding {
                    code: "size.blob",
                    message: "committed blob exceeds its configured byte limit",
                    path: Some(path),
                    size,
                    limit,
                    pattern,
                });
            }
        }
        if let Some(limit) = self.total.filter(|&limit| total_bytes > limit) {
            report.diagnostics.push(Finding {
                code: "size.total",
                message: "total stored blob bytes exceed the configured limit",
                path: None,
                size: total_bytes,
                limit,
                pattern: None,
            });
        }
        Ok(report)
    }
}
