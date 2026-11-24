//! Bounded, source-located projection used by security policy and language adapters.
//! Scalar text is stored once per composed node, even when an alias refers to it repeatedly.
use super::{
    ParsedInput, ReferenceContext,
    yaml::{Cursor, Graph, Value},
};
use libyaml_safer::ScalarStyle;
use serde::{Deserialize, Serialize};

const MAPPING: i32 = -1;
const SEQUENCE: i32 = -2;

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub(super) struct Point(pub i32, pub usize, pub usize);
impl Point {
    fn at(node: Cursor<'_>) -> Self {
        let id = match node.node().value {
            Value::Map(_) => MAPPING,
            Value::Seq(_) => SEQUENCE,
            Value::Scalar { .. } => node.edge.id as i32,
        };
        let mark = node.mark();
        Self(id, mark.line, mark.column)
    }
    pub fn mapping(self) -> bool {
        self.0 == MAPPING
    }
    pub fn scalar(self, facts: &Facts) -> Option<(&str, &str)> {
        let i = usize::try_from(self.0).ok()?;
        facts
            .scalars
            .get(i)?
            .as_ref()
            .map(|(v, t)| (v.as_str(), t.as_str()))
    }
    pub fn text(self, facts: &Facts) -> Option<&str> {
        self.scalar(facts).map(|(v, _)| v)
    }
}
#[derive(Debug, Serialize, Deserialize)]
pub(super) struct Field(pub Point, pub Vec<(Point, Point)>);
impl Field {
    fn at(node: Cursor<'_>) -> Self {
        Self(
            Point::at(node),
            node.map()
                .into_iter()
                .flatten()
                .map(|(k, v)| (Point::at(k), Point::at(v)))
                .collect(),
        )
    }
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Step {
    pub at: Point,
    pub uses: Vec<Point>,
    pub inputs: Vec<Field>,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Job {
    pub id: Point,
    pub at: Point,
    pub permissions: Vec<Field>,
    pub uses: Vec<Point>,
    pub secrets: Vec<Point>,
    pub steps: Vec<Step>,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Facts {
    pub scalars: Vec<Option<(String, String)>>,
    pub root: Point,
    pub triggers: Vec<(Point, Vec<Point>)>,
    pub permissions: Vec<Field>,
    pub jobs: Vec<Job>,
}
/// Evaluate an adapter's serialized source-located facts. The compact scalar
/// index preserves alias sharing while validating every location and reference.
pub fn check_fact_document(
    document: &str,
    rules: &[String],
) -> Result<Vec<super::Diagnostic>, String> {
    let invalid = "workflow security facts are invalid";
    if document.len() > 16 * 1024 * 1024 {
        return Err(invalid.into());
    }
    let facts: Facts = serde_json::from_str(document).map_err(|_| invalid)?;
    if facts.scalars.len() > 20_000
        || facts
            .scalars
            .iter()
            .flatten()
            .any(|(v, t)| v.chars().count() > 65_536 || t.chars().count() > 128)
    {
        return Err(invalid.into());
    }
    let valid = |p: Point| {
        p.1 != 0 && p.2 != 0 && (matches!(p.0, MAPPING | SEQUENCE) || p.scalar(&facts).is_some())
    };
    let fields = |fields: &[Field]| {
        fields.iter().all(|Field(p, pairs)| {
            valid(*p)
                && (p.mapping() || pairs.is_empty())
                && pairs.iter().all(|&(k, v)| valid(k) && valid(v))
        })
    };
    let points = |ps: &[Point]| ps.iter().all(|&p| valid(p));
    if !valid(facts.root)
        || !fields(&facts.permissions)
        || !facts
            .triggers
            .iter()
            .all(|(p, events)| valid(*p) && points(events))
        || !facts.jobs.iter().all(|j| {
            valid(j.id)
                && j.id.scalar(&facts).is_some()
                && valid(j.at)
                && j.at.mapping()
                && fields(&j.permissions)
                && points(&j.uses)
                && points(&j.secrets)
                && j.steps
                    .iter()
                    .all(|s| valid(s.at) && s.at.mapping() && points(&s.uses) && fields(&s.inputs))
        })
    {
        return Err(invalid.into());
    }
    super::security::check_facts(&facts, rules)
}
fn steps(group: Cursor<'_>, output: &mut Vec<Step>) {
    // Nested groups can be chained through aliases. Use a stack to preserve
    // source order without tying valid inputs to the native thread's stack size.
    let mut pending: Vec<_> = group.sequence().into_iter().flatten().collect();
    pending.reverse();
    while let Some(node) = pending.pop() {
        let Some(fields) = node.map() else { continue };
        let mut step = Step {
            at: Point::at(node),
            uses: Vec::new(),
            inputs: Vec::new(),
        };
        let mut children = Vec::new();
        for (k, v) in fields {
            match k.scalar() {
                Some("uses") => step.uses.push(Point::at(v)),
                Some("with") => step.inputs.push(Field::at(v)),
                Some("parallel") => children.extend(v.sequence().into_iter().flatten()),
                _ => (),
            }
        }
        output.push(step);
        pending.extend(children.into_iter().rev());
    }
}
impl Facts {
    pub fn project(graph: &Graph) -> Self {
        let root = graph.cursor(graph.root);
        let mut facts = Self {
            scalars: graph
                .nodes
                .iter()
                .map(|n| match &n.value {
                    Value::Scalar { value, tag, .. } => Some((value.to_string(), tag.clone())),
                    _ => None,
                })
                .collect(),
            root: Point::at(root),
            triggers: Vec::new(),
            permissions: Vec::new(),
            jobs: Vec::new(),
        };
        for (k, v) in root.map().into_iter().flatten() {
            match k.scalar() {
                Some("permissions") => facts.permissions.push(Field::at(v)),
                Some("on") => {
                    let events = match &v.node().value {
                        Value::Scalar { .. } => vec![Point::at(v)],
                        Value::Seq(_) => v.sequence().unwrap().map(Point::at).collect(),
                        Value::Map(_) => v.map().unwrap().map(|(k, _)| Point::at(k)).collect(),
                    };
                    facts.triggers.push((Point::at(v), events))
                }
                Some("jobs") => {
                    for (id, node) in v.map().into_iter().flatten() {
                        if id.scalar().is_none() {
                            continue;
                        }
                        let Some(fields) = node.map() else { continue };
                        let mut job = Job {
                            id: Point::at(id),
                            at: Point::at(node),
                            permissions: Vec::new(),
                            uses: Vec::new(),
                            secrets: Vec::new(),
                            steps: Vec::new(),
                        };
                        for (key, value) in fields {
                            match key.scalar() {
                                Some("permissions") => job.permissions.push(Field::at(value)),
                                Some("uses") => job.uses.push(Point::at(value)),
                                Some("secrets") => job.secrets.push(Point::at(value)),
                                Some("steps") => steps(value, &mut job.steps),
                                _ => (),
                            }
                        }
                        facts.jobs.push(job)
                    }
                }
                _ => (),
            }
        }
        facts
    }
}

#[derive(Serialize)]
pub struct Bundle<'a> {
    facts: Facts,
    references: Vec<(usize, bool, usize, usize, usize, usize)>,
    images: Vec<(usize, bool, usize, usize, Option<&'static str>)>,
    diagnostics: Vec<(&'a str, &'a str, usize, usize)>,
    node_count: usize,
}
impl ParsedInput {
    /// Project the immutable reference and security facts from this single composition.
    pub fn bundle(&self) -> Bundle<'_> {
        let references = self
            .references
            .iter()
            .map(|r| {
                let n = &self.graph.nodes[r.scalar_id];
                (
                    r.scalar_id,
                    r.context == ReferenceContext::Job,
                    r.line,
                    r.column,
                    n.at.index,
                    n.end,
                )
            })
            .collect();
        let images = self
            .images
            .iter()
            .map(|i| {
                let Value::Scalar { style, .. } = &self.graph.nodes[i.scalar_id].value else {
                    unreachable!("selected image is a scalar")
                };
                let style = match style {
                    ScalarStyle::DoubleQuoted => Some("\""),
                    ScalarStyle::SingleQuoted => Some("'"),
                    ScalarStyle::Folded => Some(">"),
                    ScalarStyle::Literal => Some("|"),
                    _ => None,
                };
                (i.scalar_id, i.service, i.at.line, i.at.column, style)
            })
            .collect();
        let diagnostics = self
            .diagnostics
            .iter()
            .map(|d| (d.code.as_str(), d.message.as_str(), d.line, d.column))
            .collect();
        Bundle {
            facts: Facts::project(&self.graph),
            references,
            images,
            diagnostics,
            node_count: self.node_count,
        }
    }
}
