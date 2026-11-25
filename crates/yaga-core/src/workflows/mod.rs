//! Bounded workflow input discovery, YAML composition and policy evaluation.
mod facts;
#[cfg(feature = "process")]
mod inputs;
pub use facts::{Bundle, check_fact_document};
mod inspect;
mod references;
mod security;
mod tags;
#[cfg(test)]
mod tests;
mod yaml;
use crate::text::safe_text;
#[cfg(feature = "process")]
pub use inputs::{
    DiscoveryLimits, HostSelection, WorkflowInput, discover_inputs, discover_inputs_with,
    load_inputs,
};
pub use references::{check_image, check_uses};
use std::sync::Arc;
#[cfg(feature = "process")]
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
};
use yaml::{Position, Value};

/// Composition limits can only tighten the public workflow input bounds.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    bytes: usize,
    nodes: usize,
    depth: usize,
    anchors: usize,
    aliases: usize,
    anchor_name: usize,
    tag_name: usize,
    scalar: usize,
    visits: usize,
    references: usize,
    diagnostics: usize,
}
impl Limits {
    pub const MAX: [usize; 11] = [
        1024 * 1024,
        20_000,
        64,
        64,
        64,
        128,
        128,
        65_536,
        50_000,
        512,
        512,
    ];
    /// Bounds are ordered as bytes, nodes, depth, anchors, aliases, anchor name,
    /// tag name, scalar characters, expanded visits, references and diagnostics.
    pub fn bounded(request: [usize; 11]) -> Self {
        let [
            bytes,
            nodes,
            depth,
            anchors,
            aliases,
            anchor_name,
            tag_name,
            scalar,
            visits,
            references,
            diagnostics,
        ] = std::array::from_fn(|i| request[i].min(Self::MAX[i]));
        Self {
            bytes,
            nodes,
            depth,
            anchors,
            aliases,
            anchor_name,
            tag_name,
            scalar,
            visits,
            references,
            diagnostics,
        }
    }
}
impl Default for Limits {
    fn default() -> Self {
        Self::bounded(Self::MAX)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, serde::Serialize)]
pub struct Diagnostic {
    pub line: usize,
    pub column: usize,
    pub code: String,
    pub message: String,
}
impl Diagnostic {
    fn new(code: &str, message: impl Into<String>, at: Position) -> Self {
        Self {
            line: at.line,
            column: at.column,
            code: code.into(),
            message: message.into(),
        }
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ReferenceContext {
    Job,
    Step,
}
fn serialize_arc_str<S: serde::Serializer>(value: &Arc<str>, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(value)
}
#[derive(Debug, serde::Serialize)]
pub struct Reference {
    #[serde(serialize_with = "serialize_arc_str")]
    pub value: Arc<str>,
    pub context: ReferenceContext,
    pub line: usize,
    pub column: usize,
    /// UTF-8 byte span of the exact value within the original BOM-stripped YAML.
    pub source_start: Option<usize>,
    pub source_end: Option<usize>,
    #[serde(skip)]
    pub(super) scalar_id: usize,
}
#[derive(Debug)]
pub struct ParsedInput {
    pub(crate) relative_path: String,
    pub(crate) references: Vec<Reference>,
    pub(crate) node_count: usize,
    graph: yaml::Graph,
    diagnostics: Vec<Diagnostic>,
    reference_diagnostics: Vec<Diagnostic>,
    images: Vec<inspect::Image>,
}
impl ParsedInput {
    pub fn relative_path(&self) -> &str {
        &self.relative_path
    }
    pub fn references(&self) -> &[Reference] {
        &self.references
    }
    pub fn node_count(&self) -> usize {
        self.node_count
    }
    pub fn references_checked(&self) -> usize {
        self.references.len() + self.images.len()
    }
    pub fn reference_findings(&self) -> Vec<Diagnostic> {
        let mut diagnostics = self.diagnostics.clone();
        diagnostics.extend_from_slice(&self.reference_diagnostics);
        diagnostics.sort();
        diagnostics
    }
    pub fn security_findings(&self, rules: &[String]) -> Result<Vec<Diagnostic>, String> {
        security::check(&self.graph, rules)
    }
}

pub fn parse_workflow(raw: &[u8], label: &str) -> Result<ParsedInput, String> {
    parse_with_limits(raw, label, Limits::default())
}
pub fn parse_with_limits(raw: &[u8], label: &str, limits: Limits) -> Result<ParsedInput, String> {
    parse_document(raw, label, limits).map_err(|e| e.message)
}

/// Leave malformed YAML in the staged snapshot for actionlint's own diagnostics.
/// Resource limits and other input failures stop staging.
#[cfg(feature = "process")]
pub(crate) fn parse_for_actionlint(raw: &[u8], label: &str) -> Result<Option<ParsedInput>, String> {
    match parse_document(raw, label, Limits::default()) {
        Ok(input) => Ok(Some(input)),
        Err(error) if error.malformed => Ok(None),
        Err(error) => Err(error.message),
    }
}

fn parse_document(raw: &[u8], label: &str, limits: Limits) -> Result<ParsedInput, yaml::Error> {
    let safe = safe_text(label, 160);
    let label = if safe.is_empty() {
        "workflow input"
    } else {
        &safe
    };
    let graph = yaml::compose(raw, label, limits)?;
    let root = graph.cursor(graph.root);
    if root.scalar() == Some("")
        && root.tagged_as("tag:yaml.org,2002:null")
        && root.mark().index == root.node().end
    {
        return Err(yaml::Error::document(label));
    }
    let syntax = graph.inspect_syntax(label).map_err(yaml::Error::input)?;
    let result = inspect::inspect(&graph, syntax, label).map_err(yaml::Error::input)?;
    Ok(ParsedInput {
        relative_path: label.into(),
        node_count: graph.nodes.len(),
        graph,
        references: result.references,
        diagnostics: result.structure,
        reference_diagnostics: result.issues,
        images: result.images,
    })
}

pub fn parse_action_manifest(
    raw: &[u8],
    label: &str,
) -> Result<(usize, Vec<(String, String)>), String> {
    parse_manifest_with_limits(raw, label, Limits::default())
}
pub fn parse_manifest_with_limits(
    raw: &[u8],
    label: &str,
    limits: Limits,
) -> Result<(usize, Vec<(String, String)>), String> {
    let safe = safe_text(label, 160);
    let label = if safe.is_empty() {
        "action manifest"
    } else {
        &safe
    };
    let graph = match yaml::compose(raw, label, limits) {
        Ok(graph) => graph,
        Err(error) if error.malformed && !error.message.ends_with("is not valid UTF-8") => {
            return Ok((0, Vec::new()));
        }
        Err(error) => return Err(error.message),
    };
    let mut dependencies = Vec::new();
    for (k, v) in graph.cursor(graph.root).map().into_iter().flatten() {
        if k.scalar() != Some("runs") {
            continue;
        }
        for (key, value) in v.map().into_iter().flatten() {
            if let (
                Some(
                    field @ ("main" | "pre" | "post" | "image" | "pre-entrypoint" | "entrypoint"
                    | "post-entrypoint"),
                ),
                Some(value),
            ) = (key.scalar(), value.scalar())
            {
                dependencies.push((field.into(), value.into()));
            }
        }
    }
    Ok((graph.nodes.len(), dependencies))
}

#[cfg(feature = "process")]
pub fn parse_inputs(inputs: &[WorkflowInput]) -> Result<Vec<ParsedInput>, String> {
    if inputs.is_empty() {
        return Err("preloaded workflow inputs must be a nonempty tuple".into());
    }
    if inputs.len() > 128 {
        return Err("preloaded workflow inputs exceed the hard 128-file limit".into());
    }
    let mut total_bytes = 0;
    let mut seen = HashSet::new();
    let mut seen_paths = HashSet::new();
    let mut root = None::<PathBuf>;
    for input in inputs {
        if !input.path.is_absolute() {
            return Err("preloaded workflow input path must be absolute and resolved".into());
        }
        let relative = Path::new(&input.relative_path);
        let label = &input.relative_path;
        let mut chars = label.chars();
        let drive = chars
            .next()
            .is_some_and(|c| !cfg!(windows) || c.len_utf16() == 1)
            && chars.next() == Some(':');
        let components = label.split('/');
        if drive
            || label.contains(['\\', '\0'])
            || label.is_empty()
            || (label != "." && (label.ends_with('/') || components.clone().any(|s| s == ".")))
            || label.contains("//") && !label.starts_with("//")
        {
            return Err(
                "preloaded workflow input path is not canonical and repository-relative".into(),
            );
        }
        if label.len() > 4096
            || components.clone().count() > 64
            || label.starts_with('/')
            || components.clone().any(|s| matches!(s, "" | "." | ".."))
        {
            return Err("workflow path is not a bounded repository-relative path".into());
        }
        if !inputs::workflow_suffix(relative) {
            return Err("preloaded workflow files must use a .yml or .yaml extension".into());
        }
        if !seen.insert(label) {
            return Err("preloaded workflow inputs contain a duplicate path".into());
        }
        if input.content.len() > inputs::MAX_WORKFLOW_BYTES {
            return Err("preloaded workflow input exceeds the hard 1048576-byte limit".into());
        }
        total_bytes += input.content.len();
        if total_bytes > 8 * inputs::MAX_WORKFLOW_BYTES {
            return Err(
                "preloaded workflow inputs exceed the hard 8388608-byte total limit".into(),
            );
        }
        if input.path.to_string_lossy().contains('\0') {
            return Err("preloaded workflow input path cannot be resolved safely".into());
        }
        let resolved = crate::fs::resolve_user_path(&input.path)
            .map_err(|_| "preloaded workflow input path cannot be resolved safely")?;
        if input.path != resolved {
            return Err("preloaded workflow input path must be absolute and resolved".into());
        }
        if !seen_paths.insert(resolved.clone()) {
            return Err("preloaded workflow inputs contain a duplicate resolved path".into());
        }
        let parent = resolved
            .ancestors()
            .nth(components.count())
            .filter(|p| p.join(relative) == resolved)
            .ok_or("preloaded workflow input path does not match its repository-relative path")?;
        if root.get_or_insert_with(|| parent.to_owned()) != parent {
            return Err("preloaded workflow inputs do not belong to one repository".into());
        }
    }
    let mut total_nodes = 0;
    let mut parsed = Vec::with_capacity(inputs.len());
    for input in inputs {
        let mut item = parse_workflow(&input.content, &input.relative_path)?;
        item.relative_path.clone_from(&input.relative_path);
        total_nodes += item.node_count;
        if total_nodes > 100_000 {
            return Err("workflow inputs exceed the hard 100000-node total limit".into());
        }
        parsed.push(item);
    }
    Ok(parsed)
}
