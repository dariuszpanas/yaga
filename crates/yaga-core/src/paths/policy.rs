//! Closed, versioned TOML policy documents. Rust callers may supply an input
//! label to retain file and field context in CLI-compatible validation errors.
use super::{
    ChangeRuleIssue, DriveSyntax, PathError, Pattern, PolicyValueError, changes, modes,
    portability,
    records::{MAX_BLOB_BYTES, ModeKind},
    sizes, tree, unique,
};
use crate::{
    text::{python_repr, safe_text},
    version::{REQUIREMENT_TYPE_ERROR, check_requirement},
};
use std::collections::HashSet;
use toml::{Table, Value};
type Result<T> = std::result::Result<T, PathError>;
fn failure(detail: impl Into<String>) -> PathError {
    PathError::Configuration(detail.into())
}

struct Document {
    table: Table,
    at: String,
    drive_syntax: DriveSyntax,
}
impl Document {
    fn decode(bytes: &[u8], family: &str, label: Option<&str>) -> Result<Table> {
        let filename = || label.map_or_else(String::new, |s| format!(": {}", safe_text(s, 300)));
        if bytes.len() > 65536 {
            return Err(failure(format!(
                "{family} policy exceeds 65536 bytes{}",
                filename()
            )));
        }
        let text = std::str::from_utf8(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes))
            .map_err(|_| failure(format!("{family} policy is not valid UTF-8{}", filename())))?;
        toml::from_str(text).map_err(|e| {
            let at = label.map_or_else(String::new, |s| format!(" in {}", safe_text(s, 300)));
            let offset = e.span().map_or(0, |s| s.start);
            let before = text.get(..offset).unwrap_or_default();
            let line =
                before.lines().count() + usize::from(before.is_empty() || before.ends_with('\n'));
            let column = before
                .rsplit('\n')
                .next()
                .unwrap_or_default()
                .chars()
                .count()
                + 1;
            let message = safe_text(e.message(), 160);
            failure(format!(
                "invalid {family} policy TOML{at}: {message} (at line {line}, column {column})"
            ))
        })
    }
    fn from_table(
        mut table: Table,
        family: &str,
        label: Option<&str>,
        required: &[&str],
        optional: &[&str],
        drive_syntax: DriveSyntax,
    ) -> Result<Self> {
        let at = label.map_or_else(String::new, |s| format!(" in {}", safe_text(s, 300)));
        if let Some(value) = table.remove("required-version") {
            let spec = value
                .as_str()
                .ok_or(PathError::Policy(REQUIREMENT_TYPE_ERROR))?;
            check_requirement(spec).map_err(failure)?;
        }
        let version = format!("{family}-policy-version");
        let schema = table.get(&version).and_then(Value::as_integer);
        let version_error = || {
            if let Some(v) = schema.filter(|&v| v > 1) {
                PathError::PolicyValue(PolicyValueError::NewerVersion {
                    key: version.clone(),
                    version: v,
                    at: at.clone(),
                })
            } else {
                failure(format!("{version} must be the integer 1{at}"))
            }
        };
        if family == "tree" && schema.is_some_and(|v| v > 1) {
            return Err(version_error());
        }
        let subject = format!("{family} policy");
        let mut keys = vec![version.as_str()];
        keys.extend_from_slice(required);
        reject_unknown(&table, &keys, optional, &subject, &at)?;
        if family == "change" || family == "path" {
            if !table.contains_key(&version) {
                let prefix = if family == "path" {
                    "required key "
                } else {
                    ""
                };
                return Err(failure(format!(
                    "{subject} is missing {prefix}{version}{at}"
                )));
            }
        } else {
            require_keys(&table, &keys, &subject, &at)?
        }
        if schema != Some(1) {
            return Err(version_error());
        }
        if family == "change" && !table.contains_key("rules") {
            return Err(failure(format!("change policy is missing rules{at}")));
        }
        Ok(Self {
            table,
            at,
            drive_syntax,
        })
    }
    fn err(&self, msg: impl std::fmt::Display) -> PathError {
        failure(format!("{msg}{}", self.at))
    }
    fn rows(&self, key: &str, maximum: usize) -> Result<&[Value]> {
        let Some(raw) = self.table.get(key) else {
            return Ok(&[]);
        };
        let rows = raw
            .as_array()
            .ok_or_else(|| self.err(format!("{key} must be an array of tables")))?;
        if rows.len() > maximum {
            return Err(self.err(format!("{key} exceeds {maximum} entries")));
        }
        Ok(rows)
    }
    fn strings<'a>(&self, raw: &'a Value, label: &str) -> Result<Vec<&'a str>> {
        raw.as_array()
            .and_then(|vs| vs.iter().map(Value::as_str).collect())
            .ok_or_else(|| self.err(format!("{label} must be an array of strings")))
    }
    fn count(&self, raw: &Value, label: &str) -> Result<u64> {
        raw.as_integer()
            .and_then(|n| u64::try_from(n).ok())
            .filter(|&n| n <= MAX_BLOB_BYTES)
            .ok_or_else(|| {
                self.err(format!(
                    "{label} must be an integer from 0 through {MAX_BLOB_BYTES}"
                ))
            })
    }
    fn checked_pattern(&self, pattern: &str, family: &str, label: &str) -> Result<()> {
        Pattern::compile_with_drive_syntax(pattern, self.drive_syntax)
            .map(|_| ())
            .map_err(|e| self.invalid(e, family, label))
    }
    fn invalid(&self, error: PathError, family: &str, label: &str) -> PathError {
        let detail = match error {
            PathError::InvalidPath => {
                format!("{family} path must be a canonical repository-relative POSIX leaf path")
            }
            _ => error
                .to_string()
                .replacen("path pattern", &format!("{family} pattern"), 1),
        };
        PathError::InvalidPolicyField {
            location: format!("{label}{}", self.at),
            detail,
        }
    }
    fn overrides<'a, T>(
        &'a self,
        key: &str,
        field: &str,
        family: &str,
        parse: impl Fn(&Value, &str) -> Result<T>,
    ) -> Result<Vec<(&'a str, T)>> {
        let mut found = Vec::new();
        let mut seen = HashSet::new();
        for (i, row) in self.rows(key, 128)?.iter().enumerate() {
            let label = format!("{key} entry {}", i + 1);
            let table = row
                .as_table()
                .ok_or_else(|| self.err(format!("{label} must be a table")))?;
            let keys = ["pattern", field];
            reject_unknown(table, &keys, &[], &label, &self.at)?;
            require_keys(table, &keys, &label, &self.at)?;
            let pattern = table["pattern"]
                .as_str()
                .ok_or_else(|| self.err(format!("{label} pattern must be a string")))?;
            if !seen.insert(pattern) {
                return Err(self.err(format!("{key} patterns must be unique and case-sensitive")));
            }
            let value = parse(&table[field], &format!("{label} {field}"))?;
            self.checked_pattern(pattern, family, &label)?;
            found.push((pattern, value));
        }
        Ok(found)
    }
}
fn reject_unknown(
    table: &Table,
    required: &[&str],
    optional: &[&str],
    label: &str,
    at: &str,
) -> Result<()> {
    let mut keys: Vec<_> = table
        .keys()
        .map(String::as_str)
        .filter(|k| !required.contains(k) && !optional.contains(k))
        .collect();
    if !keys.is_empty() {
        keys.sort_unstable();
        return Err(failure(format!(
            "unknown {label} key(s){at}: {}",
            keys.join(", ")
        )));
    }
    Ok(())
}
fn require_keys(table: &Table, required: &[&str], label: &str, at: &str) -> Result<()> {
    let mut keys: Vec<_> = required
        .iter()
        .copied()
        .filter(|k| !table.contains_key(*k))
        .collect();
    if !keys.is_empty() {
        keys.sort_unstable();
        return Err(failure(format!(
            "{label} is missing required key(s){at}: {}",
            keys.join(", ")
        )));
    }
    Ok(())
}
macro_rules! policy_loader {
    ($type:path, $family:literal, [$($required:literal),*], [$($optional:literal),*], $parse:ident) => {
        impl $type {
            pub fn from_toml(bytes: &[u8]) -> Result<Self> {
                Self::from_toml_at(bytes, None)
            }

            pub fn from_toml_at(bytes: &[u8], label: Option<&str>) -> Result<Self> {
                Self::from_toml_with_drive_syntax(bytes, label, DriveSyntax::NATIVE)
            }

            pub fn from_toml_with_drive_syntax(bytes: &[u8], label: Option<&str>, syntax: DriveSyntax) -> Result<Self> {
                let table = Document::decode(bytes, $family, label)?;
                Self::from_table_with_drive_syntax(table, label, syntax)
            }

            /// Validate a decoded TOML document with the same schema as the file loader.
            pub fn from_table_at(table: Table, label: Option<&str>) -> Result<Self> {
                Self::from_table_with_drive_syntax(table, label, DriveSyntax::NATIVE)
            }

            /// Apply the host's path semantics at the ordinary pattern-validation step.
            pub fn from_table_with_drive_syntax(table: Table, label: Option<&str>, syntax: DriveSyntax) -> Result<Self> {
                Self::document(table, label, syntax).and_then(|doc| $parse(&doc))
            }

            fn document(table: Table, label: Option<&str>, syntax: DriveSyntax) -> Result<Document> {
                Document::from_table(table, $family, label, &[$($required),*], &[$($optional),*], syntax)
            }
        }
    };
}
policy_loader!(
    tree::TreePolicy,
    "tree",
    ["required-paths", "forbidden-patterns"],
    [],
    tree_policy
);
fn tree_policy(doc: &Document) -> Result<tree::TreePolicy> {
    let (required, forbidden) = tree_fields(doc)?;
    tree::TreePolicy::with_drive_syntax(&required, &forbidden, doc.drive_syntax)
        .map_err(|e| doc.invalid(e, "tree", "tree policy"))
}

impl tree::TreePolicy {
    /// Validate the wire document for a host which constructs its own tree policy model.
    pub fn document_fields(
        table: Table,
        label: Option<&str>,
    ) -> Result<(Vec<String>, Vec<String>)> {
        let doc = Self::document(table, label, DriveSyntax::NATIVE)?;
        let (required, forbidden) = tree_fields(&doc)?;
        Ok((
            required.into_iter().map(str::to_owned).collect(),
            forbidden.into_iter().map(str::to_owned).collect(),
        ))
    }
}

fn tree_fields(doc: &Document) -> Result<(Vec<&str>, Vec<&str>)> {
    let strings = |key: &str| {
        let values = doc.strings(&doc.table[key], key)?;
        if values.len() > 128 {
            return Err(doc.err(format!("{key} exceeds 128 entries")));
        }
        if !unique(&values) {
            return Err(doc.err(format!("{key} must contain unique case-sensitive values")));
        }
        Ok(values)
    };
    let required = strings("required-paths")?;
    let forbidden = strings("forbidden-patterns")?;
    if !(1..=128).contains(&(required.len() + forbidden.len())) {
        return Err(doc.err(
            "required-paths and forbidden-patterns must contain 1 through 128 combined entries",
        ));
    }
    Ok((required, forbidden))
}
policy_loader!(
    sizes::SizePolicy,
    "size",
    ["default-max-blob-bytes"],
    ["max-total-blob-bytes", "path-limits"],
    size_policy
);
fn size_policy(doc: &Document) -> Result<sizes::SizePolicy> {
    let default = doc.count(
        &doc.table["default-max-blob-bytes"],
        "default-max-blob-bytes",
    )?;
    let total = doc
        .table
        .get("max-total-blob-bytes")
        .map(|v| doc.count(v, "max-total-blob-bytes"))
        .transpose()?;
    let overrides = doc.overrides("path-limits", "max-blob-bytes", "size", |v, k| {
        doc.count(v, k)
    })?;
    sizes::SizePolicy::with_drive_syntax(default, total, &overrides, doc.drive_syntax)
}
policy_loader!(
    modes::ModePolicy,
    "mode",
    ["default-allowed-modes"],
    ["path-overrides"],
    mode_policy
);
fn mode_policy(doc: &Document) -> Result<modes::ModePolicy> {
    let allowed = |value: &Value, key: &str| -> Result<Vec<ModeKind>> {
        let all = value
            .as_array()
            .filter(|a| !a.is_empty())
            .ok_or_else(|| doc.err(format!("{key} must be a nonempty array of mode names")))?;
        let names = all
            .iter()
            .map(Value::as_str)
            .collect::<Option<Vec<_>>>()
            .ok_or_else(|| doc.err(format!("{key} must contain only strings")))?;
        if !unique(&names) {
            return Err(doc.err(format!("{key} must be unique")));
        }
        names
            .into_iter()
            .map(|name| {
                [
                    ModeKind::Regular,
                    ModeKind::Executable,
                    ModeKind::Symlink,
                    ModeKind::Gitlink,
                ]
                .into_iter()
                .find(|k| k.name() == name)
                .ok_or_else(|| {
                    PathError::PolicyValue(PolicyValueError::UnknownMode {
                        field: key.to_owned(),
                        name: name.to_owned(),
                        at: doc.at.clone(),
                    })
                })
            })
            .collect()
    };
    let default = allowed(&doc.table["default-allowed-modes"], "default-allowed-modes")?;
    let overrides = doc.overrides("path-overrides", "allowed-modes", "mode", allowed)?;
    let overrides = overrides
        .iter()
        .map(|(p, k)| (*p, k.as_slice()))
        .collect::<Vec<_>>();
    modes::ModePolicy::with_drive_syntax(&default, &overrides, doc.drive_syntax)
}
policy_loader!(
    portability::PathPolicy,
    "path",
    [],
    ["profile", "rules"],
    path_policy
);
fn path_policy(doc: &Document) -> Result<portability::PathPolicy> {
    match (doc.table.get("profile"), doc.table.get("rules")) {
        (Some(profile), None) if profile.as_str() == Some("windows-compatible-v1") => {
            Ok(portability::PathPolicy::windows_compatible_v1())
        }
        (Some(_), None) => Err(doc.err("profile must be 'windows-compatible-v1'")),
        (None, Some(raw)) => {
            let names = doc.strings(raw, "rules")?;
            if !(1..=4).contains(&names.len()) {
                return Err(doc.err("rules must contain 1 through 4 entries"));
            }
            if !unique(&names) {
                return Err(doc.err("rules must contain unique case-sensitive values"));
            }
            let rules = names
                .iter()
                .map(|s| {
                    portability::Rule::ALL
                        .into_iter()
                        .find(|r| r.name() == *s)
                        .ok_or_else(|| failure(format!("unknown path policy rule{}: {s}", doc.at)))
                })
                .collect::<Result<Vec<_>>>()?;
            portability::PathPolicy::new(&rules)
        }
        _ => Err(doc.err("path policy must define exactly one of profile or rules")),
    }
}
policy_loader!(
    changes::ChangePolicy,
    "change",
    ["rules"],
    [],
    change_policy
);
fn change_policy(doc: &Document) -> Result<changes::ChangePolicy> {
    let rows = doc.table["rules"]
        .as_array()
        .filter(|rows| rows.iter().all(Value::is_table))
        .ok_or_else(|| doc.err("rules must be an array of tables"))?;
    if rows.is_empty() {
        return Err(doc.err("rules must not be empty"));
    }
    if rows.len() > 32 {
        return Err(doc.err("rules exceeds 32 entries"));
    }
    let mut fields = Vec::new();
    let mut seen = HashSet::new();
    for (i, row) in rows.iter().enumerate() {
        let label = format!("rules[{}]", i + 1);
        let row = row.as_table().expect("checked rule table");
        let keys = ["name", "when-any", "require-any"];
        reject_unknown(row, &keys, &[], &label, &doc.at)?;
        require_keys(row, &keys, &label, &doc.at)?;
        let name = row["name"]
            .as_str()
            .ok_or_else(|| doc.err(format!("{label}.name must be a string")))?;
        let patterns = |key: &str| {
            let field = format!("{label}.{key}");
            let names = doc.strings(&row[key], &field)?;
            if names.is_empty() {
                return Err(doc.err(format!("{field} must not be empty")));
            }
            if names.len() > 16 {
                return Err(doc.err(format!("{field} exceeds 16 entries")));
            }
            if !unique(&names) {
                return Err(doc.err(format!("{field} patterns must be unique")));
            }
            Ok(names)
        };
        let when_any = patterns("when-any")?;
        let require_any = patterns("require-any")?;
        let invalid_rule = |issue| PathError::InvalidChangeRule {
            index: i + 1,
            at: doc.at.clone(),
            issue,
        };
        if !changes::valid_name(name) {
            return Err(invalid_rule(ChangeRuleIssue::Name));
        }
        for pattern in when_any.iter().chain(&require_any) {
            Pattern::compile_with_drive_syntax(pattern, doc.drive_syntax)
                .map_err(|e| invalid_rule(ChangeRuleIssue::Pattern(Box::new(e))))?;
        }
        if !seen.insert(name) {
            return Err(doc.err(format!(
                "rules contains duplicate name {}",
                python_repr(name)
            )));
        }
        fields.push((name, when_any, require_any))
    }
    let rules = fields
        .iter()
        .map(|(name, when_any, require_any)| changes::Rule {
            name,
            when_any,
            require_any,
        })
        .collect::<Vec<_>>();
    changes::ChangePolicy::with_drive_syntax(&rules, doc.drive_syntax)
}
