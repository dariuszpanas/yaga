use super::*;
use std::cell::{Cell, RefCell};

const CHILD_TAG: &str = "5b9a36f1e80c477c8251527ec84dbc6f";
const VOLUME_TAG: &str = "0830c97fa7bf4078bb71c46fe3612bd6";
const ID: &str = "100000000000000000000000000000000000000000000000000000000000000a";

struct Daemon {
    origin: Instant,
    time: Cell<Duration>,
    child_created_at: Duration,
    child_id: &'static str,
    child: Cell<bool>,
    volume: Cell<bool>,
    reserved: bool,
    removable: bool,
    removed: RefCell<Vec<String>>,
    lookups: Cell<usize>,
    lookup_fault: Option<(usize, &'static str)>,
    removal_fault: Option<&'static str>,
}

impl Daemon {
    fn new(appearance: u64) -> Self {
        Self {
            origin: Instant::now(),
            time: Cell::new(Duration::from_secs(30)),
            child_created_at: Duration::from_secs(appearance),
            child_id: ID,
            child: Cell::new(true),
            volume: Cell::new(true),
            reserved: true,
            removable: true,
            removed: RefCell::default(),
            lookups: Cell::new(0),
            lookup_fault: None,
            removal_fault: None,
        }
    }
}

impl Runtime for Daemon {
    fn now(&self) -> Instant {
        self.origin + self.time.get()
    }
    fn pause(&self, duration: Duration) {
        assert!(duration > Duration::ZERO);
        self.time.set(self.time.get() + duration);
    }
    fn lookup(&self, res: &Resource, deadline: Instant) -> Result<Option<String>, LifecycleError> {
        assert!(self.now() < deadline);
        self.lookups.set(self.lookups.get() + 1);
        if let Some((index, error)) = self.lookup_fault
            && self.lookups.get() == index
        {
            return Err(error.into());
        }
        if res.kind == Kind::Volume {
            assert_eq!(res.tag, VOLUME_TAG);
            return Ok(self.volume.get().then(|| res.name.clone()));
        }
        assert_eq!(res.tag, CHILD_TAG);
        assert_eq!(res.name, res.kind.name(CHILD_TAG));
        Ok(
            (self.child.get() && self.time.get() >= self.child_created_at)
                .then(|| self.child_id.to_owned()),
        )
    }
    fn remove(&self, kind: Kind, id: &str, deadline: Instant) -> Result<(), &'static str> {
        assert!(self.now() < deadline);
        if kind == Kind::Volume {
            assert_eq!(id, Kind::Volume.name(VOLUME_TAG));
            if self.reserved && self.child.get() {
                return Err("volume in use");
            }
            self.volume.set(false);
        } else {
            assert_eq!(id, self.child_id);
            if !self.removable {
                return Err("container in use");
            }
            self.child.set(false);
        }
        self.removed.borrow_mut().push(id.to_owned());
        self.removal_fault.map_or(Ok(()), Err)
    }
}

#[test]
fn timed_out_create_is_reaped_during_parent_cleanup() {
    for kind in [Kind::Stage, Kind::Lint] {
        let daemon = Daemon::new(47);
        let pending = Cell::default();
        let mut volume = Guard::new(&daemon, &pending, Kind::Volume, VOLUME_TAG);
        volume.resource.id = Some(volume.resource.name.clone());
        // The Docker client returned a timeout at t=30. The API actually
        // completes at t=47, after the child's ten-second visibility grace.
        let mut child = Guard::new(&daemon, &pending, kind, CHILD_TAG);
        child.finish().unwrap();
        assert_eq!(daemon.time.get(), Duration::from_secs(40));
        assert!(daemon.removed.borrow().is_empty());
        volume.finish().unwrap();
        assert_eq!(daemon.time.get(), Duration::from_secs(47));
        assert_eq!(
            *daemon.removed.borrow(),
            [ID, volume.resource.name.as_str()]
        );
        assert!(!daemon.volume.get() && !daemon.child.get());
        assert!(pending.take().is_none());
    }
}

#[test]
fn parent_keeps_watching_when_volume_disappears_first() {
    let mut daemon = Daemon::new(47);
    daemon.reserved = false;
    let pending = Cell::default();
    let mut child = Guard::new(&daemon, &pending, Kind::Stage, CHILD_TAG);
    child.finish().unwrap();
    let mut volume = Guard::new(&daemon, &pending, Kind::Volume, VOLUME_TAG);
    volume.resource.id = Some(volume.resource.name.clone());
    volume.finish().unwrap();
    assert_eq!(daemon.time.get(), Duration::from_secs(47));
    assert_eq!(
        *daemon.removed.borrow(),
        [volume.resource.name.as_str(), ID]
    );
    assert!(!daemon.volume.get() && !daemon.child.get());
}

#[test]
fn successful_create_removes_only_verified_resources_without_polling() {
    let daemon = Daemon::new(0);
    let pending = Cell::default();
    let mut child = Guard::new(&daemon, &pending, Kind::Lint, CHILD_TAG);
    child.resource.id = Some(ID.into());
    child.finish().unwrap();
    let mut volume = Guard::new(&daemon, &pending, Kind::Volume, VOLUME_TAG);
    volume.resource.id = Some(volume.resource.name.clone());
    volume.finish().unwrap();
    assert_eq!(daemon.time.get(), Duration::from_secs(30));
    assert_eq!(daemon.lookups.get(), 4);
    assert_eq!(
        *daemon.removed.borrow(),
        [ID, volume.resource.name.as_str()]
    );
}

#[test]
fn mismatched_confirmed_container_is_never_removed() {
    let daemon = Daemon::new(0);
    let pending = Cell::default();
    let mut child = Guard::new(&daemon, &pending, Kind::Stage, CHILD_TAG);
    child.resource.id = Some("f".repeat(64));
    assert!(
        child
            .finish()
            .unwrap_err()
            .contains("unexpected actionlint resource identity")
    );
    let mut volume = Guard::new(&daemon, &pending, Kind::Volume, VOLUME_TAG);
    volume.resource.id = Some(volume.resource.name.clone());
    assert!(
        volume
            .finish()
            .unwrap_err()
            .contains("unexpected actionlint resource identity")
    );
    assert!(daemon.removed.borrow().is_empty());
}

#[test]
fn unremovable_late_child_fails_within_the_original_parent_deadline() {
    let mut daemon = Daemon::new(47);
    daemon.removable = false;
    let pending = Cell::default();
    let mut child = Guard::new(&daemon, &pending, Kind::Lint, CHILD_TAG);
    child.finish().unwrap();
    let mut volume = Guard::new(&daemon, &pending, Kind::Volume, VOLUME_TAG);
    volume.resource.id = Some(volume.resource.name.clone());
    let error = volume.finish().unwrap_err();
    assert!(
        error.contains("execution container lookup: Docker operation exceeded its hard timeout before client launch"),
        "{error}"
    );
    assert!(
        error.contains("Preceding execution container removal: container in use"),
        "{error}"
    );
    assert_eq!(daemon.time.get(), Duration::from_secs(50));
    assert!(daemon.removed.borrow().is_empty());
}

#[test]
fn failed_lookup_and_failed_confirmation_identify_each_resource_role() {
    for (kind, role) in [
        (Kind::Volume, "workspace volume"),
        (Kind::Stage, "staging container"),
        (Kind::Lint, "execution container"),
    ] {
        for (index, operation) in [(1, "lookup"), (2, "removal confirmation")] {
            let mut daemon = Daemon::new(0);
            daemon.reserved = false;
            daemon.lookup_fault = Some((index, "injected lookup fault"));
            daemon.removal_fault = Some("injected removal fault");
            let tag = if kind == Kind::Volume {
                VOLUME_TAG
            } else {
                CHILD_TAG
            };
            let pending = Cell::default();
            let mut guard = Guard::new(&daemon, &pending, kind, tag);
            let error = guard.finish().unwrap_err();
            assert!(
                error.contains(&format!("{role} {operation}: injected lookup fault")),
                "{error}"
            );
            assert_eq!(
                error.contains(&format!("Preceding {role} removal: injected removal fault")),
                index == 2
            );
            assert_eq!(daemon.lookups.get(), index);
            assert_eq!(daemon.time.get(), Duration::from_secs(30));
        }
    }
}

#[test]
fn unavailable_child_lookup_retains_the_last_unconfirmed_volume_removal() {
    let daemon = Daemon::new(99);
    let pending = Cell::default();
    let mut child = Guard::new(&daemon, &pending, Kind::Stage, CHILD_TAG);
    child.finish().unwrap();
    let mut volume = Guard::new(&daemon, &pending, Kind::Volume, VOLUME_TAG);
    volume.resource.id = Some(volume.resource.name.clone());
    let error = volume.finish().unwrap_err();
    assert!(error.contains("staging container lookup: Docker operation exceeded its hard timeout before client launch"), "{error}");
    assert!(
        error.contains("Preceding workspace volume removal: volume in use"),
        "{error}"
    );
    assert_eq!(daemon.time.get(), Duration::from_secs(50));
    assert!(daemon.removed.borrow().is_empty());
}

#[test]
fn confirmed_absence_overrides_removal_client_failure() {
    let mut daemon = Daemon::new(0);
    daemon.removal_fault = Some("Docker runtime timed out after client launch");
    let pending = Cell::default();
    let mut child = Guard::new(&daemon, &pending, Kind::Stage, CHILD_TAG);
    child.resource.id = Some(ID.into());
    child.finish().unwrap();
    assert!(child.resource.removal_failure.is_none());
    assert!(pending.take().is_none());
    assert_eq!(daemon.lookups.get(), 2);
    assert_eq!(daemon.time.get(), Duration::from_secs(30));
}
