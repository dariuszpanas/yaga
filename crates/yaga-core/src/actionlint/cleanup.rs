//! Release only Docker resources owned by this invocation, within its existing cleanup budgets.

use super::LifecycleError;
use std::time::{Duration, Instant};

pub(super) const CLEANUP: Duration = Duration::from_secs(10);
const POLL: Duration = Duration::from_millis(100);

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Kind {
    Volume,
    Stage,
    Lint,
}

impl Kind {
    pub(super) fn name(self, tag: &str) -> String {
        let prefix = match self {
            Self::Volume => "",
            Self::Stage => "stage-",
            Self::Lint => "lint-",
        };
        format!("yaga-actionlint-{prefix}{tag}")
    }

    pub(super) fn noun(self) -> &'static str {
        if self == Self::Volume {
            "volume"
        } else {
            "container"
        }
    }

    fn role(self) -> &'static str {
        match self {
            Self::Volume => "workspace volume",
            Self::Stage => "staging container",
            Self::Lint => "execution container",
        }
    }

    pub(super) fn creation(self) -> &'static str {
        match self {
            Self::Volume => "create the actionlint workspace volume",
            Self::Stage => "create the actionlint staging container",
            Self::Lint => "create the actionlint execution container",
        }
    }
}

#[derive(Clone)]
pub(super) struct Resource {
    pub kind: Kind,
    pub name: String,
    pub tag: String,
    /// Identity from the create response or the first matching private-label lookup.
    pub id: Option<String>,
    // Relevant only while this resource's absence remains unconfirmed.
    removal_failure: Option<&'static str>,
}

impl Resource {
    pub(super) fn new(kind: Kind, tag: &str) -> Self {
        Self {
            kind,
            name: kind.name(tag),
            tag: tag.into(),
            id: None,
            removal_failure: None,
        }
    }

    fn failure(&self, operation: &str, reason: &str) -> String {
        let mut diagnostic = format!("{} {operation}: {reason}", self.kind.role());
        self.append_removal_failure(&mut diagnostic);
        diagnostic
    }

    fn append_removal_failure(&self, diagnostic: &mut String) {
        if let Some(error) = self.removal_failure {
            diagnostic.push_str(&format!(
                ". Preceding {} removal: {error}",
                self.kind.role()
            ));
        }
    }
}

pub(super) trait Runtime {
    fn embedded(&self) -> bool {
        false
    }
    fn now(&self) -> Instant {
        Instant::now()
    }
    fn pause(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
    fn lookup(
        &self,
        resource: &Resource,
        deadline: Instant,
    ) -> Result<Option<String>, LifecycleError>;
    fn remove(&self, kind: Kind, id: &str, deadline: Instant) -> Result<(), &'static str>;
}

fn sweep(
    runtime: &impl Runtime,
    resource: &mut Resource,
    deadline: Instant,
) -> Result<bool, LifecycleError> {
    let Some(id) = runtime.lookup(resource, deadline).map_err(|e| {
        if runtime.embedded() {
            e
        } else {
            resource.failure("lookup", &e.to_string()).into()
        }
    })?
    else {
        resource.removal_failure = None;
        return Ok(true);
    };
    if *resource.id.get_or_insert_with(|| id.clone()) != id {
        if runtime.embedded() {
            return Err("Docker returned a different actionlint container ID".into());
        }
        return Err(resource
            .failure(
                "lookup",
                "Docker returned an unexpected actionlint resource identity",
            )
            .into());
    }
    // The daemon may have removed it concurrently, or its child may still hold it.
    // Only a fresh lookup can confirm removal after either success or failure.
    resource.removal_failure = runtime.remove(resource.kind, &id, deadline).err();
    let remaining = runtime.lookup(resource, deadline).map_err(|e| {
        if runtime.embedded() {
            e
        } else {
            resource
                .failure("removal confirmation", &e.to_string())
                .into()
        }
    })?;
    if remaining.as_ref().is_some_and(|other| *other != id) {
        if runtime.embedded() {
            return Err("Docker returned a different actionlint container ID".into());
        }
        return Err(resource
            .failure(
                "removal confirmation",
                "Docker returned an unexpected actionlint resource identity",
            )
            .into());
    }
    if remaining.is_none() {
        resource.removal_failure = None;
    }
    Ok(remaining.is_none())
}

/// An uncertain child that never appeared remains its parent's responsibility.
pub(super) fn clean(
    runtime: &impl Runtime,
    resource: &mut Resource,
    late: bool,
    mut child: Option<&mut Resource>,
) -> Result<(), LifecycleError> {
    let start = runtime.now();
    let grace = if late { start + CLEANUP } else { start };
    let deadline = grace + CLEANUP;
    loop {
        if runtime.now() >= deadline {
            if runtime.embedded() {
                return Err("Docker actionlint cleanup exceeded its hard timeout".into());
            }
            let next = child.as_deref().unwrap_or(resource);
            let mut message = next.failure(
                "lookup",
                "Docker operation exceeded its hard timeout before client launch",
            );
            // A still-unconfirmed child is checked before its parent on each
            // pass. The parent's most recent removal can also remain unverified.
            if child.is_some() {
                resource.append_removal_failure(&mut message);
            }
            return Err(message.into());
        }
        let mut child_present = false;
        if let Some(pending) = child.as_deref_mut() {
            let absent = sweep(runtime, pending, deadline).map_err(|e| {
                if runtime.embedded() {
                    e
                } else {
                    format!(
                        "could not verify private actionlint child {}: {e}",
                        pending.name
                    )
                    .into()
                }
            })?;
            child_present = !absent;
            if absent && pending.id.is_some() {
                child = None;
            }
        }
        if child_present {
            runtime.pause(deadline.saturating_duration_since(runtime.now()).min(POLL));
            continue;
        }
        let absent = sweep(runtime, resource, deadline)?;
        let now = runtime.now();
        if absent && now >= grace {
            // If an unacknowledged child was never visible, keep checking it
            // after the volume disappears, within the original volume budget.
            if child.is_none() || deadline.saturating_duration_since(now) <= POLL {
                return Ok(());
            }
        }
        runtime.pause(deadline.saturating_duration_since(now).min(POLL));
    }
}

pub(super) struct Guard<'a, R: Runtime> {
    runtime: &'a R,
    pending: &'a std::cell::Cell<Option<Resource>>,
    pub resource: Resource,
    active: bool,
}

impl<'a, R: Runtime> Guard<'a, R> {
    pub(super) fn new(
        runtime: &'a R,
        pending: &'a std::cell::Cell<Option<Resource>>,
        kind: Kind,
        tag: &str,
    ) -> Self {
        Self {
            runtime,
            pending,
            resource: Resource::new(kind, tag),
            active: true,
        }
    }

    #[cfg(test)]
    pub(super) fn finish(&mut self) -> Result<(), String> {
        self.finish_detailed(self.resource.id.is_none())
            .map_err(|(message, _)| message)
    }

    pub(super) fn finish_detailed(&mut self, late: bool) -> Result<(), (String, LifecycleError)> {
        let is_volume = self.resource.kind == Kind::Volume;
        let mut pending = if is_volume { self.pending.take() } else { None };
        let result = clean(self.runtime, &mut self.resource, late, pending.as_mut());
        if !is_volume && (self.runtime.embedded() || result.is_err() || self.resource.id.is_none())
        {
            // No next workflow starts after this failure. Carry only this exact
            // child into its parent, so it can be reaped if the daemon answers late.
            self.pending.set(Some(self.resource.clone()));
        }
        self.active = false;
        result.map_err(|cause| {
            let what = if !self.runtime.embedded() {
                "private actionlint resource"
            } else if is_volume {
                "private actionlint workspace"
            } else {
                "actionlint container"
            };
            let detail = if self.runtime.embedded() {
                crate::text::safe_text(&cause.to_string(), 200)
            } else {
                cause.to_string()
            };
            (
                format!(
                    "Docker could not remove {what} {}: {detail}",
                    self.resource.name
                ),
                cause,
            )
        })
    }
}

impl<R: Runtime> Drop for Guard<'_, R> {
    fn drop(&mut self) {
        if self.active {
            let _ = self.finish_detailed(self.resource.id.is_none());
        }
    }
}

#[cfg(test)]
mod tests;
