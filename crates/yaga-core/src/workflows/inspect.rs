use super::yaml::{Cursor, Graph};
use super::{Diagnostic, Limits, Reference, ReferenceContext, Value, references};

#[derive(Debug)]
pub(super) struct Image {
    pub scalar_id: usize,
    pub at: super::Position,
    pub service: bool,
}

pub(super) struct Inspection {
    pub references: Vec<Reference>,
    pub structure: Vec<Diagnostic>,
    pub issues: Vec<Diagnostic>,
    pub images: Vec<Image>,
    limits: Limits,
}

pub(super) fn inspect(
    graph: &Graph,
    syntax: Vec<Diagnostic>,
    label: &str,
) -> Result<Inspection, String> {
    let mut inspector = Inspection {
        references: Vec::new(),
        structure: syntax,
        issues: Vec::new(),
        images: Vec::new(),
        limits: graph.limits,
    };
    let root = graph.cursor(graph.root);
    if let Some(pairs) = root.map() {
        let mut saw_jobs = false;
        for (key, value) in pairs {
            match key.scalar() {
                None => inspector.diagnose(key, "workflow mapping keys must be scalars", label)?,
                Some("jobs") => {
                    saw_jobs = true;
                    inspector.jobs(value, label)?;
                }
                _ => (),
            }
        }
        if !saw_jobs {
            inspector.diagnose(root, "workflow document must define a jobs mapping", label)?;
        }
    } else {
        inspector.diagnose(root, "workflow document root must be a mapping", label)?;
    }
    inspector.references.sort_by_key(|r| (r.line, r.column));
    inspector.images.sort_by_key(|i| (i.at.line, i.at.column));
    inspector.structure.sort();
    Ok(inspector)
}

impl Inspection {
    fn diagnose(&mut self, field: Cursor<'_>, message: &str, label: &str) -> Result<(), String> {
        if self.structure.len() >= self.limits.diagnostics {
            return Err(format!(
                "{label} exceeds the hard workflow diagnostic limit of {}",
                self.limits.diagnostics
            ));
        }
        self.structure
            .push(Diagnostic::new("workflow.structure", message, field.mark()));
        Ok(())
    }
    fn capacity(&self, label: &str) -> Result<(), String> {
        if self.images.len() + self.references.len() >= self.limits.references {
            return Err(format!(
                "{label} exceeds the hard workflow reference limit of {}",
                self.limits.references
            ));
        }
        Ok(())
    }
    fn jobs(&mut self, jobs: Cursor<'_>, label: &str) -> Result<(), String> {
        let Some(pairs) = jobs.map() else {
            return self.diagnose(jobs, "jobs must be a mapping", label);
        };
        for (id, job) in pairs {
            if id.scalar().is_none() {
                self.diagnose(id.direct(), "job identifiers must be scalars", label)?;
                continue;
            }
            let Some(fields) = job.map() else {
                self.diagnose(job, "job definitions must be mappings", label)?;
                continue;
            };
            for (key, value) in fields {
                match key.scalar() {
                    None => {
                        self.diagnose(key.direct(), "job mapping keys must be scalars", label)?
                    }
                    Some("uses") => self.reference(value.direct(), ReferenceContext::Job, label)?,
                    Some("steps") => self.steps(value.direct(), label)?,
                    Some("container") => self.container(value, label)?,
                    Some("services") => self.services(value, label)?,
                    _ => (),
                }
            }
        }
        Ok(())
    }
    fn steps(&mut self, steps: Cursor<'_>, label: &str) -> Result<(), String> {
        if steps.sequence().is_none() {
            return self.diagnose(steps, "job steps must be a sequence", label);
        }
        let mut pending = vec![steps];
        while let Some(group) = pending.pop() {
            for step in group.sequence().unwrap() {
                let Some(fields) = step.map() else {
                    self.diagnose(step.direct(), "step definitions must be mappings", label)?;
                    continue;
                };
                for (key, value) in fields {
                    match key.scalar() {
                        None => {
                            self.diagnose(key.direct(), "step mapping keys must be scalars", label)?
                        }
                        Some("uses") => {
                            self.reference(value.direct(), ReferenceContext::Step, label)?
                        }
                        Some("parallel") if value.sequence().is_some() => {
                            pending.push(value.direct())
                        }
                        Some("parallel") => self.diagnose(
                            value.direct(),
                            "step parallel group must be a sequence",
                            label,
                        )?,
                        _ => (),
                    }
                }
            }
        }
        Ok(())
    }
    fn reference(
        &mut self,
        node: Cursor<'_>,
        context: ReferenceContext,
        label: &str,
    ) -> Result<(), String> {
        let Value::Scalar {
            value,
            literal_offset,
            ..
        } = &node.node().value
        else {
            return self.diagnose(
                node,
                if context == ReferenceContext::Step {
                    "step uses value must be a scalar"
                } else {
                    "job uses value must be a scalar"
                },
                label,
            );
        };
        self.capacity(label)?;
        let at = node.mark();
        if let Some((code, message)) = references::check_uses(value, context) {
            self.issues.push(Diagnostic::new(code, message, at));
        }
        self.references.push(Reference {
            scalar_id: node.edge.id,
            value: value.clone(),
            context,
            line: at.line,
            column: at.column,
            source_start: *literal_offset,
            source_end: literal_offset.map(|o| o + value.len()),
        });
        Ok(())
    }
    fn image(&mut self, node: Cursor<'_>, service: bool, label: &str) -> Result<(), String> {
        let Value::Scalar {
            value, tag, quoted, ..
        } = &node.node().value
        else {
            return self.diagnose(
                node,
                if service {
                    "service image must be a scalar"
                } else {
                    "job container image must be a scalar"
                },
                label,
            );
        };
        self.capacity(label)?;
        self.images.push(Image {
            scalar_id: node.edge.id,
            at: node.mark(),
            service,
        });
        if let Some((code, message)) = references::check_image(value, tag, *quoted, service) {
            self.issues
                .push(Diagnostic::new(code, message, node.mark()));
        }
        Ok(())
    }
    fn container(&mut self, value: Cursor<'_>, label: &str) -> Result<(), String> {
        if value.scalar().is_some() {
            return self.image(value, false, label);
        }
        let Some(entries) = value.map() else {
            return self.diagnose(value, "job container must be a scalar or mapping", label);
        };
        for (key, v) in entries {
            match key.scalar() {
                None => self.diagnose(key, "job container mapping keys must be scalars", label)?,
                Some("image") => self.image(v, false, label)?,
                _ => (),
            }
        }
        Ok(())
    }
    fn services(&mut self, node: Cursor<'_>, label: &str) -> Result<(), String> {
        let Some(entries) = node.map() else {
            return self.diagnose(node, "job services must be a mapping", label);
        };
        for (id, service) in entries {
            if id.scalar().is_none() {
                self.diagnose(id, "service identifiers must be scalars", label)?;
                continue;
            }
            let Some(entries) = service.map() else {
                self.diagnose(service, "service definitions must be mappings", label)?;
                continue;
            };
            for (key, value) in entries {
                match key.scalar() {
                    None => self.diagnose(key, "service mapping keys must be scalars", label)?,
                    Some("image") => self.image(value, true, label)?,
                    _ => (),
                }
            }
        }
        Ok(())
    }
}
