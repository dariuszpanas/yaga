//! Compose source-marked YAML without constructing arbitrary tagged values or
//! expanding aliases into owned copies. Document-wide limits precede inspection.
use super::{Diagnostic, Limits};
use crate::text::safe_text;
use libyaml_safer::{EventData, Mark, Parser, ScalarStyle};
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Position {
    pub line: usize,
    pub column: usize,
    pub index: usize,
}
impl From<Mark> for Position {
    fn from(mark: Mark) -> Self {
        Self {
            line: mark.line as usize + 1,
            column: mark.column as usize + 1,
            index: mark.index as usize,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Edge {
    pub id: usize,
    pub at: Position,
}
#[derive(Debug)]
pub(super) enum Value {
    Scalar {
        value: Arc<str>,
        tag: String,
        style: ScalarStyle,
        quoted: bool,
        literal_offset: Option<usize>,
    },
    Map(Vec<(Edge, Edge)>),
    Seq(Vec<Edge>),
}
#[derive(Debug)]
pub(super) struct Node {
    pub at: Position,
    pub value: Value,
    pub end: usize,
    explicit: Option<String>,
}
#[derive(Debug)]
pub(super) struct Graph {
    pub nodes: Vec<Node>,
    pub root: Edge,
    pub limits: Limits,
}
#[derive(Clone, Copy)]
pub(super) struct Cursor<'a> {
    graph: &'a Graph,
    pub edge: Edge,
    inherited: Option<Position>,
}
impl Graph {
    pub fn cursor(&self, edge: Edge) -> Cursor<'_> {
        Cursor {
            graph: self,
            edge,
            inherited: None,
        }
    }
}
impl<'a> Cursor<'a> {
    pub fn mark(self) -> Position {
        self.inherited.unwrap_or(self.edge.at)
    }
    pub fn node(self) -> &'a Node {
        &self.graph.nodes[self.edge.id]
    }
    pub fn scalar(self) -> Option<&'a str> {
        match &self.node().value {
            Value::Scalar { value, .. } => Some(value),
            _ => None,
        }
    }
    fn child(self, edge: Edge) -> Self {
        let inherited = self
            .inherited
            .or_else(|| (self.edge.at != self.node().at).then_some(self.edge.at));
        Self {
            graph: self.graph,
            edge,
            inherited,
        }
    }
    pub fn direct(self) -> Self {
        self.graph.cursor(self.edge)
    }
    pub fn map(self) -> Option<impl Iterator<Item = (Self, Self)> + 'a> {
        let Value::Map(pairs) = &self.node().value else {
            return None;
        };
        Some(
            pairs
                .iter()
                .map(move |&(key, value)| (self.child(key), self.child(value))),
        )
    }
    pub fn sequence(self) -> Option<impl Iterator<Item = Self> + 'a> {
        let Value::Seq(items) = &self.node().value else {
            return None;
        };
        Some(items.iter().map(move |&e| self.child(e)))
    }
    pub fn tagged_as(self, tag: &str) -> bool {
        matches!(&self.node().value,Value::Scalar{tag:t,..} if t==tag)
    }
}

pub(super) struct Error {
    pub message: String,
    pub malformed: bool,
}
impl Error {
    pub(super) fn input(message: String) -> Self {
        Self {
            message,
            malformed: false,
        }
    }
    fn limit(label: &str, detail: impl std::fmt::Display) -> Self {
        Self::input(format!("{label} {detail}"))
    }
    fn malformed(label: &str) -> Self {
        Self {
            message: format!("{label} contains invalid YAML"),
            malformed: true,
        }
    }
    pub(super) fn document(label: &str) -> Self {
        Self {
            message: format!("{label} must contain exactly one nonempty YAML document"),
            malformed: true,
        }
    }
}

struct Frame {
    id: usize,
    key: Option<Edge>,
}

pub(super) fn compose(raw: &[u8], label: &str, limits: Limits) -> Result<Graph, Error> {
    if raw.len() > limits.bytes {
        return Err(Error::limit(
            label,
            format!("exceeds the hard {}-byte limit", limits.bytes),
        ));
    }
    let raw = raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(raw);
    let text = std::str::from_utf8(raw).map_err(|_| Error {
        message: format!("{label} is not valid UTF-8"),
        malformed: true,
    })?;
    let offsets =
        (!text.is_ascii()).then(|| text.char_indices().map(|(i, _)| i).collect::<Vec<_>>());
    let character_index = |byte: usize| {
        offsets
            .as_ref()
            .map_or(byte, |indices| indices.partition_point(|&i| i < byte))
    };
    let mut parser = Parser::new();
    parser.set_input(std::io::Cursor::new(raw));
    let mut nodes = Vec::<Node>::new();
    let mut stack = Vec::<Frame>::new();
    let mut root = None;
    let mut documents = 0;
    let mut anchors = HashMap::new();
    let mut aliases = 0;
    loop {
        let event = parser.parse().map_err(|_| Error::malformed(label))?;
        let start_byte = event.start_mark.index as usize;
        let end_byte = event.end_mark.index as usize;
        let at = Position {
            index: character_index(start_byte),
            ..Position::from(event.start_mark)
        };
        let end = character_index(end_byte);
        let collection = matches!(
            event.data,
            EventData::SequenceStart { .. } | EventData::MappingStart { .. }
        );
        let edge = match event.data {
            EventData::StreamStart { .. } | EventData::DocumentEnd { .. } => continue,
            EventData::DocumentStart { .. } => {
                documents += 1;
                if documents > 1 {
                    return Err(Error::document(label));
                }
                continue;
            }
            EventData::StreamEnd => break,
            EventData::SequenceEnd | EventData::MappingEnd => {
                stack.pop().ok_or_else(|| Error::malformed(label))?;
                continue;
            }
            _ if stack.len() >= limits.depth => {
                return Err(Error::limit(
                    label,
                    format!("exceeds the hard YAML depth limit of {}", limits.depth),
                ));
            }
            EventData::Alias { anchor } => {
                aliases += 1;
                if aliases > limits.aliases {
                    return Err(Error::limit(
                        label,
                        format!("exceeds the hard alias limit of {}", limits.aliases),
                    ));
                }
                if anchor.chars().count() > limits.anchor_name {
                    return Err(Error::limit(
                        label,
                        format!(
                            "contains an anchor or alias name longer than {} characters",
                            limits.anchor_name
                        ),
                    ));
                }
                Edge {
                    id: *anchors
                        .get(&anchor)
                        .ok_or_else(|| Error::malformed(label))?,
                    at,
                }
            }
            event => {
                let id = nodes.len();
                if id >= limits.nodes {
                    return Err(Error::limit(
                        label,
                        format!("exceeds the hard node limit of {}", limits.nodes),
                    ));
                }
                let is_map = matches!(event, EventData::MappingStart { .. });
                let (anchor, tag, scalar) = match event {
                    EventData::Scalar {
                        anchor,
                        tag,
                        value,
                        style,
                        ..
                    } => (anchor, tag, Some((value, style))),
                    EventData::SequenceStart { anchor, tag, .. }
                    | EventData::MappingStart { anchor, tag, .. } => (anchor, tag, None),
                    _ => return Err(Error::malformed(label)),
                };
                if let Some(name) = anchor {
                    if anchors.len() >= limits.anchors {
                        return Err(Error::limit(
                            label,
                            format!("exceeds the hard anchor limit of {}", limits.anchors),
                        ));
                    }
                    if name.chars().count() > limits.anchor_name {
                        return Err(Error::limit(
                            label,
                            format!(
                                "contains an anchor or alias name longer than {} characters",
                                limits.anchor_name
                            ),
                        ));
                    }
                    if anchors.insert(name, id).is_some() {
                        return Err(Error::malformed(label));
                    }
                }
                if tag
                    .as_ref()
                    .is_some_and(|s| s.chars().count() > limits.tag_name)
                {
                    return Err(Error::limit(
                        label,
                        format!(
                            "contains a tag name longer than {} characters",
                            limits.tag_name
                        ),
                    ));
                }
                let value = if let Some((scalar, style)) = scalar {
                    if scalar.chars().count() > limits.scalar {
                        return Err(Error::limit(
                            label,
                            format!("contains a scalar longer than {} characters", limits.scalar),
                        ));
                    }
                    let scalar_tag = tag
                        .clone()
                        .filter(|t| t != "!")
                        .unwrap_or_else(|| super::tags::resolve(&scalar, style));
                    let quoted =
                        matches!(style, ScalarStyle::SingleQuoted | ScalarStyle::DoubleQuoted);
                    // The lint snapshot may rewrite a commit-bound literal only when it has one
                    // exact occurrence within this scalar's original source span.
                    let literal_offset = if scalar.starts_with("$/") {
                        text.get(start_byte..end_byte).and_then(|raw_scalar| {
                            let mut found = raw_scalar.match_indices(&scalar);
                            let (index, _) = found.next()?;
                            found.next().is_none().then_some(start_byte + index)
                        })
                    } else {
                        None
                    };
                    Value::Scalar {
                        value: Arc::from(scalar),
                        tag: scalar_tag,
                        style,
                        quoted,
                        literal_offset,
                    }
                } else if is_map {
                    Value::Map(Vec::new())
                } else {
                    Value::Seq(Vec::new())
                };
                nodes.push(Node {
                    value,
                    at,
                    end,
                    explicit: tag,
                });
                Edge { id, at }
            }
        };
        if let Some(parent) = stack.last_mut() {
            match &mut nodes[parent.id].value {
                Value::Seq(items) => items.push(edge),
                Value::Map(pairs) => {
                    if let Some(key) = parent.key.take() {
                        pairs.push((key, edge))
                    } else {
                        parent.key = Some(edge)
                    }
                }
                _ => return Err(Error::malformed(label)),
            }
        } else if root.replace(edge).is_some() {
            return Err(Error::document(label));
        }
        if collection {
            stack.push(Frame {
                id: edge.id,
                key: None,
            })
        }
    }
    let root = root.ok_or_else(|| Error::document(label))?;
    Ok(Graph {
        nodes,
        root,
        limits,
    })
}

impl Graph {
    pub fn inspect_syntax(&self, label: &str) -> Result<Vec<Diagnostic>, String> {
        let mut active = vec![false; self.nodes.len()];
        let mut seen = vec![false; self.nodes.len()];
        let mut visits = 0;
        let mut pending = vec![(self.root.id, false)];
        let mut diagnostics = Vec::new();
        while let Some((id, exiting)) = pending.pop() {
            if exiting {
                active[id] = false;
                continue;
            }
            visits += 1;
            if visits > self.limits.visits {
                return Err(format!(
                    "{label} exceeds the hard expanded-node visit limit of {}",
                    self.limits.visits
                ));
            }
            if active[id] {
                return Err(format!("{label} contains an alias cycle"));
            }
            active[id] = true;
            pending.push((id, true));
            let node = &self.nodes[id];
            if !seen[id] {
                seen[id] = true;
                if let Some(tag) = &node.explicit {
                    diagnostics.push(Diagnostic::new(
                        "yaml.tag",
                        format!("explicit YAML tag is not supported: {}", safe_text(tag, 80)),
                        node.at,
                    ));
                }
                if let Value::Map(pairs) = &node.value {
                    let mut keys = HashSet::new();
                    for (key, _) in pairs {
                        if let Value::Scalar { tag, value, .. } = &self.nodes[key.id].value {
                            if !keys.insert((tag, value)) {
                                diagnostics.push(Diagnostic::new(
                                    "yaml.duplicate_key",
                                    format!("duplicate YAML mapping key: {}", safe_text(value, 80)),
                                    key.at,
                                ));
                            }
                            if tag == "tag:yaml.org,2002:merge" {
                                diagnostics.push(Diagnostic::new(
                                    "yaml.merge_key",
                                    "YAML merge keys are not supported",
                                    key.at,
                                ));
                            }
                            if diagnostics.len() > self.limits.diagnostics {
                                return Err(format!(
                                    "{label} exceeds the hard workflow diagnostic limit of {}",
                                    self.limits.diagnostics
                                ));
                            }
                        }
                    }
                }
            }
            if diagnostics.len() > self.limits.diagnostics {
                return Err(format!(
                    "{label} exceeds the hard workflow diagnostic limit of {}",
                    self.limits.diagnostics
                ));
            }
            match &node.value {
                Value::Map(pairs) => {
                    for (k, v) in pairs.iter().rev() {
                        pending.push((v.id, false));
                        pending.push((k.id, false));
                    }
                }
                Value::Seq(items) => pending.extend(items.iter().rev().map(|e| (e.id, false))),
                _ => (),
            }
        }
        Ok(diagnostics)
    }
}
