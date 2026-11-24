use super::cleanup::{self, Kind, Resource};
use super::{LifecycleError, VettedDocker};
use crate::git::process::{ProcessError, run_observed};
use crate::text::safe_text;
use std::cell::Cell;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const IMAGE: &str =
    "rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667";
const LABEL: &str = "io.yaga.actionlint.run";
const STDERR: usize = 64 * 1024;
const STDOUT: usize = 2 * 1024 * 1024;
const CONTROL: usize = 1024;
const STEP: Duration = Duration::from_secs(30);

pub(super) struct Docker<'a> {
    cancelled: &'a dyn Fn() -> bool,
    executable: PathBuf,
    config: Option<PathBuf>,
    environment: Option<&'a [(OsString, OsString)]>,
    workspace: Workspace,
    // Each Docker instance owns exactly one invocation and workspace volume.
    pending_child: Cell<Option<Resource>>,
}

enum Workspace {
    Owned(tempfile::TempDir),
    Borrowed(PathBuf),
}
impl Workspace {
    fn path(&self) -> &Path {
        match self {
            Self::Owned(dir) => dir.path(),
            Self::Borrowed(path) => path,
        }
    }
}

fn token() -> Result<String, String> {
    let mut bytes = [0; 16];
    getrandom::fill(&mut bytes)
        .map_err(|_| "could not create private actionlint workspace name")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn is_id(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn safe_error(bytes: &[u8]) -> String {
    match std::str::from_utf8(bytes) {
        Ok(text) => {
            let s = safe_text(text, 200);
            if s.is_empty() {
                "the Docker runtime failed".into()
            } else {
                s
            }
        }
        Err(_) => "the Docker runtime returned non-UTF-8 error output".into(),
    }
}
enum InvokeFailure {
    Interrupted,
    Deadline,
    Client(ProcessError),
    ClientIo { code: i32, started: bool },
}

impl InvokeFailure {
    fn io_error(&self, message: &str, executable: &Path) -> LifecycleError {
        match self {
            Self::ClientIo { code, started } => LifecycleError::ClientIo {
                message: message.to_owned(),
                os_code: *code,
                executable: (!started).then(|| executable.to_path_buf()),
            },
            _ => message.into(),
        }
    }
    fn control_error(&self, operation: &str, executable: &Path) -> LifecycleError {
        match self {
            Self::Deadline | Self::Client(ProcessError::Timeout) => {
                format!("Docker timed out while attempting to {operation}").into()
            }
            Self::Client(ProcessError::StderrLimit | ProcessError::StdoutLimit) => {
                format!("Docker exceeded its output limit while attempting to {operation}").into()
            }
            _ => self.io_error(self.message(), executable),
        }
    }

    fn lint_error(self, path: &str, executable: &Path) -> LifecycleError {
        let display = safe_text(path, 200);
        match self {
            Self::Interrupted => LifecycleError::Interrupted,
            Self::Deadline => "Docker timed out while attempting to run actionlint".into(),
            Self::Client(ProcessError::Timeout) => {
                format!("actionlint timed out within its per-workflow limit for {display}").into()
            }
            Self::Client(ProcessError::StdoutLimit) => {
                format!("actionlint output exceeds the hard {STDOUT}-byte limit for {display}")
                    .into()
            }
            Self::Client(ProcessError::StderrLimit) => format!(
                "actionlint error output exceeds the hard {STDERR}-byte limit for {display}"
            )
            .into(),
            Self::Client(ProcessError::Io(_)) | Self::ClientIo { .. } => self.io_error(
                "could not execute the Docker actionlint runtime",
                executable,
            ),
        }
    }
    fn message(&self) -> &'static str {
        match self {
            Self::Interrupted => "actionlint interrupted",
            Self::Deadline => "Docker operation exceeded its hard timeout",
            Self::Client(ProcessError::Timeout) => "Docker runtime timed out",
            Self::Client(ProcessError::StderrLimit | ProcessError::StdoutLimit) => {
                "Docker runtime exceeded its output limit"
            }
            Self::Client(ProcessError::Io(_)) | Self::ClientIo { .. } => {
                "Docker control process could not be executed"
            }
        }
    }

    fn cleanup_message(&self) -> &'static str {
        match self {
            Self::Deadline => "Docker operation exceeded its hard timeout before client launch",
            Self::Client(ProcessError::Timeout) => "Docker runtime timed out after client launch",
            _ => self.message(),
        }
    }
}

fn remaining(deadline: Instant) -> Result<Duration, InvokeFailure> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or(InvokeFailure::Deadline)
}
fn control_output(out: Output, operation: &str) -> Result<Vec<u8>, String> {
    if !out.status.success() {
        return Err(format!(
            "Docker could not {operation}: {}",
            safe_error(&out.stderr)
        ));
    }
    Ok(out.stdout)
}
fn check_interrupt(cancelled: &dyn Fn() -> bool) -> Result<(), LifecycleError> {
    if cancelled() {
        Err(LifecycleError::Interrupted)
    } else {
        Ok(())
    }
}

fn finish<T>(
    outcome: Result<T, LifecycleError>,
    cleanup: Result<(), (String, LifecycleError)>,
) -> Result<T, LifecycleError> {
    match cleanup {
        Ok(()) => outcome,
        Err((message, cause)) => Err(LifecycleError::Cleanup {
            previous: outcome.err().map(Box::new),
            message,
            cause: Box::new(cause),
        }),
    }
}

impl<'a> Docker<'a> {
    pub fn from_vetted(
        repo: &Path,
        workspace: &Path,
        docker: &'a VettedDocker,
        cancelled: &'a dyn Fn() -> bool,
    ) -> Result<Self, String> {
        let (executable, config) = docker.validate(repo)?;
        let error = "actionlint temporary workspace could not be managed safely";
        if !workspace.is_absolute() {
            return Err(error.into());
        }
        let workspace = crate::fs::absolute_path(workspace).map_err(|_| error)?;
        if workspace.read_dir().map_err(|_| error)?.next().is_some() {
            return Err(error.into());
        }
        let workspace = if workspace.starts_with(repo) {
            // TMPDIR may belong to the checkout. Use a directory that this
            // call created exclusively, beneath the host's configured tempdir.
            let mut builder = tempfile::Builder::new();
            builder.prefix("yaga-actionlint-");
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                builder.permissions(std::fs::Permissions::from_mode(0o700));
            }
            Workspace::Owned(builder.tempdir_in(workspace).map_err(|_| error)?)
        } else {
            Workspace::Borrowed(workspace)
        };
        Ok(Self::new(
            executable,
            config,
            Some(&docker.environment),
            cancelled,
            workspace,
        ))
    }

    pub fn discover(repo: &Path, cancelled: &'a dyn Fn() -> bool) -> Result<Self, String> {
        let name = if cfg!(windows) {
            "docker.exe"
        } else {
            "docker"
        };
        let executable = std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
            .filter(|p| p.is_absolute())
            .map(|p| p.join(name))
            .find_map(|p| super::embedded::trusted_executable(repo, &p))
            .ok_or("Docker is required for workflow linting")?;
        let config = std::env::var_os("DOCKER_CONFIG")
            .as_deref()
            .map(|path| super::embedded::trusted_config(repo, Path::new(path)))
            .transpose()?;
        let workspace = tempfile::Builder::new()
            .prefix("yaga-actionlint-")
            .tempdir()
            .map_err(|_| "actionlint temporary workspace could not be managed safely")?;
        Ok(Self::new(
            executable,
            config,
            None,
            cancelled,
            Workspace::Owned(workspace),
        ))
    }

    fn new(
        executable: PathBuf,
        config: Option<PathBuf>,
        environment: Option<&'a [(OsString, OsString)]>,
        cancelled: &'a dyn Fn() -> bool,
        workspace: Workspace,
    ) -> Self {
        Self {
            cancelled,
            executable,
            config,
            environment,
            workspace,
            pending_child: Cell::default(),
        }
    }

    fn invoke(
        &self,
        args: &[&str],
        data: Vec<u8>,
        limit: usize,
        deadline: Instant,
        interruptible: bool,
    ) -> Result<Output, InvokeFailure> {
        let mut cmd = Command::new(&self.executable);
        if let Some(environment) = self.environment {
            cmd.env_clear()
                .envs(environment.iter().map(|(k, v)| (k, v)));
        }
        cmd.args(args).current_dir(self.workspace.path()).env(
            "PATH",
            self.executable.parent().expect("absolute Docker path"),
        );
        if let Some(config) = &self.config {
            cmd.env("DOCKER_CONFIG", config);
        } else {
            cmd.env_remove("DOCKER_CONFIG");
        }
        let interrupted = || (self.cancelled)() && interruptible;
        let mut started = false;
        run_observed(
            cmd,
            Some(data),
            limit,
            STDERR,
            remaining(deadline)?,
            || started = true,
            interrupted,
        )
        .map_err(|error| match &error {
            ProcessError::Io(io)
                if io.kind() == std::io::ErrorKind::Interrupted && interrupted() =>
            {
                InvokeFailure::Interrupted
            }
            ProcessError::Io(io) if io.raw_os_error().is_some() => InvokeFailure::ClientIo {
                code: io.raw_os_error().unwrap(),
                started,
            },
            _ => InvokeFailure::Client(error),
        })
    }
    fn workload(
        &self,
        args: &[&str],
        data: Vec<u8>,
        limit: usize,
        deadline: Instant,
        operation: &str,
    ) -> Result<Output, LifecycleError> {
        self.invoke(args, data, limit, deadline, true)
            .map_err(|e| match e {
                InvokeFailure::Interrupted => LifecycleError::Interrupted,
                _ if self.environment.is_some() => e.control_error(operation, &self.executable),
                _ => format!("Docker could not {operation}: {}", e.message()).into(),
            })
    }

    fn control(
        &self,
        args: &[&str],
        data: Vec<u8>,
        deadline: Instant,
        operation: &str,
    ) -> Result<Vec<u8>, LifecycleError> {
        Ok(control_output(
            self.workload(args, data, CONTROL, deadline, operation)?,
            operation,
        )?)
    }

    fn owned_resource<T>(
        &self,
        kind: Kind,
        tag: &str,
        create: &[&str],
        deadline: Instant,
        action: impl FnOnce(&str) -> Result<T, LifecycleError>,
    ) -> Result<T, LifecycleError> {
        // Start cleanup ownership before sending create. A daemon may create a
        // resource even after the client loses its reply.
        let mut guard = cleanup::Guard::new(self, &self.pending_child, kind, tag);
        let embedded = self.environment.is_some();
        let operation = if embedded {
            if kind == Kind::Volume {
                "create the actionlint workspace"
            } else {
                "create an actionlint container"
            }
        } else {
            kind.creation()
        };
        let created = self
            .control(create, vec![], deadline, operation)
            .and_then(|raw| {
                if embedded && !raw.is_ascii() {
                    return Err(LifecycleError::NonAsciiControl(raw));
                }
                let id = std::str::from_utf8(&raw)
                    .map_err(|_| "Docker returned non-ASCII control output")?;
                let id = id
                    .strip_suffix("\r\n")
                    .or_else(|| id.strip_suffix('\n'))
                    .unwrap_or(id);
                if embedded && (id.is_empty() || id.contains(['\n', '\r'])) {
                    return Err("Docker returned malformed control output".into());
                }
                if (kind == Kind::Volume && id != guard.resource.name)
                    || (kind != Kind::Volume && !is_id(id))
                {
                    return Err(match (embedded, kind) {
                        (false, _) => "Docker returned an unexpected actionlint resource identity",
                        (true, Kind::Volume) => {
                            "Docker returned an unexpected actionlint workspace name"
                        }
                        _ => "Docker returned a malformed actionlint container ID",
                    }
                    .into());
                }
                Ok(id.to_owned())
            });
        guard.resource.id = created.as_ref().ok().cloned();
        let uncertain = created.is_err();
        let outcome = created.and_then(|id| action(&id));
        let outcome = finish(outcome, guard.finish_detailed(uncertain));
        if uncertain && embedded {
            // The importable API has a second enclosing cleanup region around
            // each create. Confirm ownership again without extending its grace.
            return finish(outcome, guard.finish_detailed(false));
        }
        outcome
    }

    pub fn run<'p>(
        &self,
        archive: Vec<u8>,
        snapshot: &super::snapshot::Snapshot,
        selected: impl Iterator<Item = &'p str>,
        deadline: Instant,
        mut decode: impl FnMut(&str, &[u8]) -> Result<usize, String>,
    ) -> Result<(), LifecycleError> {
        check_interrupt(self.cancelled)?;
        self.check_budget(deadline)?;
        let tag = token()?;
        let volume = Kind::Volume.name(&tag);
        let label = format!("{LABEL}={tag}");
        self.owned_resource(
            Kind::Volume,
            &tag,
            &["volume", "create", "--label", &label, &volume],
            deadline.min(Instant::now() + STEP),
            |volume| {
                check_interrupt(self.cancelled)?;
                self.check_budget(deadline)?;
                self.stage(volume, archive, deadline)?;
                self.pending_child.take();
                let mut count = 0usize;
                for path in selected {
                    check_interrupt(self.cancelled)?;
                    self.check_budget(deadline)?;
                    let content = &snapshot.files[path];
                    let output =
                        self.lint_workflow(volume, path, content, snapshot.config(), deadline)?;
                    let code = output.status.code();
                    if !matches!(code, Some(0 | 1)) {
                        return Err(format!(
                            "actionlint failed for {}: {}",
                            safe_text(path, 200),
                            safe_error(&output.stderr)
                        )
                        .into());
                    }
                    let findings = decode(path, &output.stdout)?;
                    if findings > 512 {
                        return Err(
                            "actionlint findings exceed the hard 512-diagnostic limit".into()
                        );
                    }
                    if (code == Some(0)) != (findings == 0) {
                        return Err("actionlint returned an inconsistent result".into());
                    }
                    self.pending_child.take();
                    count = count.saturating_add(findings);
                    if count > 512 {
                        return Err(
                            "actionlint findings exceed the hard 512-diagnostic total limit".into(),
                        );
                    }
                }
                check_interrupt(self.cancelled)?;
                Ok(())
            },
        )
    }
    fn check_budget(&self, deadline: Instant) -> Result<(), LifecycleError> {
        if self.environment.is_some() && Instant::now() >= deadline {
            return Err("actionlint exceeded the hard 300-second total timeout".into());
        }
        Ok(())
    }
    fn stage(
        &self,
        volume: &str,
        archive: Vec<u8>,
        deadline: Instant,
    ) -> Result<(), LifecycleError> {
        let deadline = deadline.min(Instant::now() + STEP);
        let tag = token()?;
        let name = Kind::Stage.name(&tag);
        let label = format!("{LABEL}={tag}");
        let mount = format!("type=volume,source={volume},target=/workspace,volume-nocopy");
        let args = [
            "create",
            "--name",
            &name,
            "--label",
            &label,
            "--network",
            "none",
            "--read-only",
            "--log-driver",
            "none",
            "--mount",
            &mount,
            "--workdir",
            "/workspace",
            IMAGE,
            "-version",
        ];
        self.owned_resource(Kind::Stage, &tag, &args, deadline, |id| {
            self.control(
                &["cp", "-", &format!("{id}:/workspace")],
                archive,
                deadline,
                "stage the actionlint workspace",
            )?;
            Ok(())
        })
    }
    fn lint_workflow(
        &self,
        volume: &str,
        path: &str,
        content: &[u8],
        config: Option<&str>,
        deadline: Instant,
    ) -> Result<Output, LifecycleError> {
        let deadline = deadline.min(Instant::now() + STEP);
        let tag = token()?;
        let name = Kind::Lint.name(&tag);
        let label = format!("{LABEL}={tag}");
        let mount = format!("type=volume,source={volume},target=/workspace,readonly,volume-nocopy");
        let mut args = vec![
            "create",
            "--rm",
            "--name",
            &name,
            "--label",
            &label,
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--log-driver",
            "none",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "64",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1",
            "--mount",
            &mount,
            "--workdir",
            "/workspace",
            IMAGE,
            "-no-color",
            "-format",
            "{{json .}}",
        ];
        if let Some(path) = config {
            args.extend(["-config-file", path]);
        }
        args.extend(["-stdin-filename", path, "-"]);
        self.owned_resource(Kind::Lint, &tag, &args, deadline, |id| {
            let args = ["start", "--attach", "--interactive", id];
            if self.environment.is_none() {
                return self.workload(
                    &args,
                    content.to_vec(),
                    STDOUT,
                    deadline,
                    "run the actionlint execution container",
                );
            }
            self.invoke(&args, content.to_vec(), STDOUT, deadline, true)
                .map_err(|e| e.lint_error(path, &self.executable))
        })
    }
}

// Cancellation stops work. Ownership checks and cleanup retain their own deadlines.
impl cleanup::Runtime for Docker<'_> {
    fn embedded(&self) -> bool {
        self.environment.is_some()
    }

    fn lookup(
        &self,
        resource: &Resource,
        deadline: Instant,
    ) -> Result<Option<String>, LifecycleError> {
        let label = format!("label={LABEL}={}", resource.tag);
        let name_filter;
        let args = if resource.kind == Kind::Volume {
            vec!["volume", "ls", "--quiet", "--filter", &label]
        } else {
            // Docker's container-name filter is a regexp over the leading slash.
            // Both name and tag are generated locally from a fixed prefix and random hex.
            name_filter = format!("name=^/{}$", resource.name);
            vec![
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                &label,
                "--filter",
                &name_filter,
            ]
        };
        let operation = match (self.embedded(), resource.kind) {
            (true, Kind::Volume) => "inspect the actionlint workspace",
            (true, _) => "inspect actionlint containers",
            (false, _) => "inspect actionlint resources",
        };
        let raw = control_output(
            self.invoke(&args, vec![], CONTROL, deadline, false)
                .map_err(|e| {
                    if self.embedded() {
                        return if matches!(e, InvokeFailure::Deadline) {
                            "Docker actionlint cleanup exceeded its hard timeout".into()
                        } else {
                            e.control_error(operation, &self.executable)
                        };
                    }
                    format!(
                        "Docker could not inspect actionlint resources: {}",
                        e.cleanup_message()
                    )
                    .into()
                })?,
            operation,
        )?;
        parse_listing(resource, &raw, self.embedded())
    }

    fn remove(&self, kind: Kind, id: &str, deadline: Instant) -> Result<(), &'static str> {
        let out = self
            .invoke(
                &[kind.noun(), "rm", "--force", id],
                vec![],
                CONTROL,
                deadline,
                false,
            )
            .map_err(|e| e.cleanup_message())?;
        // A failed removal may still be confirmed by a later lookup. Retain
        // only a fixed outcome, never a copy of the removal client's stderr.
        out.status
            .success()
            .then_some(())
            .ok_or("Docker removal client exited unsuccessfully")
    }
}

fn parse_listing(
    resource: &Resource,
    raw: &[u8],
    embedded: bool,
) -> Result<Option<String>, LifecycleError> {
    if !raw.is_ascii() {
        return Err(LifecycleError::NonAsciiControl(raw.to_vec()));
    }
    let text = std::str::from_utf8(raw).unwrap();
    // Python's splitlines accepts these extra ASCII separators in control responses.
    let lines: Vec<&str> = if embedded {
        text.split(['\n', '\r', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e'])
            .collect()
    } else {
        text.lines().collect()
    };
    let mut ids = lines.into_iter().filter(|line| !line.is_empty());
    if embedded
        && ids
            .clone()
            .any(|line| line.bytes().any(|b| b.is_ascii_whitespace() || b == 0x1f))
    {
        return Err("Docker returned malformed control output".into());
    }
    let id = ids.next();
    if ids.next().is_some()
        || id.is_some_and(|id| match resource.kind {
            Kind::Volume => id != resource.name,
            _ => !is_id(id),
        })
    {
        return Err(match (embedded, resource.kind) {
            (true, Kind::Volume) => "Docker returned malformed actionlint workspace state",
            (true, _) => "Docker returned malformed actionlint container state",
            (false, _) => "Docker returned malformed actionlint resource state",
        }
        .into());
    }
    Ok(id.map(str::to_owned))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    #[test]
    #[ignore = "launched as the foreground client by the cancellation test"]
    fn waiting_docker_client() {
        std::fs::write("client-started", b"").unwrap();
        std::thread::sleep(Duration::from_secs(30));
    }

    #[test]
    fn cancels_a_running_client_but_allows_cleanup_clients_to_finish() {
        let cancel = AtomicBool::new(false);
        let docker = Docker {
            executable: std::env::current_exe().unwrap(),
            config: None,
            environment: None,
            workspace: Workspace::Owned(tempfile::tempdir().unwrap()),
            pending_child: Cell::default(),
            cancelled: &|| cancel.load(Ordering::Relaxed),
        };
        let marker = docker.workspace.path().join("client-started");
        let limit = Instant::now() + Duration::from_secs(10);
        let fixture = "actionlint::runtime::tests::waiting_docker_client";
        std::thread::scope(|scope| {
            scope.spawn(|| {
                while !marker.is_file() {
                    assert!(Instant::now() < limit, "fixture did not start");
                    std::thread::sleep(Duration::from_millis(5));
                }
                cancel.store(true, Ordering::Relaxed);
            });
            let error = docker
                .workload(
                    &["--ignored", "--exact", fixture],
                    vec![],
                    CONTROL,
                    limit,
                    "run the actionlint execution container",
                )
                .unwrap_err();
            assert_eq!(error, LifecycleError::Interrupted);
        });
        // Cleanup uses the same contained process runner with its independent deadline.
        // The cancellation flag is still set when this client starts.
        let output = docker
            .invoke(
                &["--list", "--exact", fixture],
                vec![],
                CONTROL,
                Instant::now() + Duration::from_secs(10),
                false,
            )
            .map_err(|e| e.message())
            .unwrap();
        assert!(output.status.success());
        assert!(String::from_utf8(output.stdout).unwrap().contains(fixture));
    }

    #[test]
    fn cleanup_distinguishes_deadlines_from_client_timeouts() {
        let workspace = tempfile::tempdir().unwrap();
        let docker = Docker {
            cancelled: &|| false,
            executable: workspace.path().join("must-not-be-launched"),
            workspace: Workspace::Owned(workspace),
            config: None,
            environment: None,
            pending_child: Cell::default(),
        };
        let deadline = Instant::now();
        let resource = Resource::new(Kind::Stage, "fixture");
        assert_eq!(
            cleanup::Runtime::lookup(&docker, &resource, deadline)
                .unwrap_err()
                .to_string(),
            "Docker could not inspect actionlint resources: Docker operation exceeded its hard timeout before client launch"
        );
        assert_eq!(
            cleanup::Runtime::remove(&docker, resource.kind, "fixture-id", deadline).unwrap_err(),
            "Docker operation exceeded its hard timeout before client launch"
        );
        let timeout = InvokeFailure::Client(ProcessError::Timeout);
        assert_eq!(
            timeout.cleanup_message(),
            "Docker runtime timed out after client launch"
        );
        assert_eq!(
            InvokeFailure::Deadline.message(),
            "Docker operation exceeded its hard timeout"
        );
        assert_eq!(timeout.message(), "Docker runtime timed out");
    }

    #[test]
    fn only_one_valid_resource_identity_is_accepted() {
        let volume = Resource::new(Kind::Volume, "private");
        let container = Resource::new(Kind::Stage, "private");
        let id = "f".repeat(64);
        assert_eq!(
            parse_listing(&volume, volume.name.as_bytes(), false).unwrap(),
            Some(volume.name.clone())
        );
        assert_eq!(
            parse_listing(&container, format!("{id}\r\n").as_bytes(), false).unwrap(),
            Some(id.clone())
        );
        for bad in [format!("{id}\n{id}"), "short".into(), "F".repeat(64)] {
            assert!(parse_listing(&container, bad.as_bytes(), false).is_err());
        }
        assert!(parse_listing(&volume, b"unowned-volume", false).is_err());
        assert!(parse_listing(&container, &[0xff], false).is_err());
        let bad = parse_listing(&container, b"secret\xff", true).unwrap_err();
        assert!(matches!(&bad,LifecycleError::NonAsciiControl(bytes) if bytes == b"secret\xff"));
        assert!(!format!("{bad:?}").contains("secret"));
    }

    #[test]
    fn primary_error_survives_a_secondary_cleanup_error() {
        let primary = format!(
            "Docker could not {}: Docker runtime timed out",
            Kind::Stage.creation()
        );
        let cleanup = "Docker could not remove private actionlint resource yaga-actionlint-test";
        let error = finish::<()>(
            Err(primary.clone().into()),
            Err((cleanup.into(), "lookup failed".into())),
        )
        .unwrap_err();
        assert_eq!(error.to_string(), format!("{primary}. {cleanup}"));
        assert_eq!(
            error,
            LifecycleError::Cleanup {
                previous: Some(Box::new(primary.into())),
                message: cleanup.into(),
                cause: Box::new("lookup failed".into())
            }
        );
    }
}
