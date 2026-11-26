//! Shell-free process execution with bounded output, a shared wall-clock deadline,
//! and cleanup of descendants which inherited the pipes.

use std::fmt;
use std::io::{self, Read, Write};
use std::process::{Command, ExitStatus, Output, Stdio};
use std::sync::mpsc::{self, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[cfg(unix)]
#[path = "unix.rs"]
mod platform;
#[cfg(windows)]
#[path = "windows.rs"]
mod platform;
use platform::Contained;

const TICK: Duration = Duration::from_millis(2);
const CLEANUP: Duration = Duration::from_secs(1);

#[cfg(any(target_os = "macos", test))]
pub(super) enum GroupKill {
    Done,
    PermissionDenied(io::Error),
}

#[cfg(any(target_os = "macos", test))]
pub(super) fn terminate_macos_group(
    deadline: Instant,
    tick: Duration,
    mut now: impl FnMut() -> Instant,
    mut kill_group: impl FnMut() -> io::Result<GroupKill>,
    mut exited: impl FnMut() -> io::Result<bool>,
    mut sleep: impl FnMut(Duration),
) -> io::Result<()> {
    loop {
        match kill_group()? {
            GroupKill::Done => return Ok(()),
            GroupKill::PermissionDenied(error) => {
                if exited()? {
                    return Ok(());
                }
                let remaining = deadline.saturating_duration_since(now());
                if remaining.is_zero() {
                    return Err(error);
                }
                sleep(tick.min(remaining));
            }
        }
    }
}

#[derive(Debug)]
pub enum ProcessError {
    Timeout,
    StdoutLimit,
    StderrLimit,
    Io(io::Error),
}

impl From<io::Error> for ProcessError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl fmt::Display for ProcessError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Timeout => f.write_str("process exceeded its time limit"),
            Self::StdoutLimit => f.write_str("process exceeded its stdout limit"),
            Self::StderrLimit => f.write_str("process exceeded its stderr limit"),
            Self::Io(error) => error.fmt(f),
        }
    }
}
impl std::error::Error for ProcessError {}

enum Finished {
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    Stdin,
}

fn capture(
    reader: impl Read,
    limit: usize,
    overflow: ProcessError,
) -> Result<Vec<u8>, ProcessError> {
    let mut bytes = Vec::new();
    reader
        .take((limit as u64).saturating_add(1))
        .read_to_end(&mut bytes)?;
    if bytes.len() > limit {
        Err(overflow)
    } else {
        Ok(bytes)
    }
}

fn worker<F>(
    workers: &mut Vec<JoinHandle<()>>,
    done: &mpsc::Sender<Result<Finished, ProcessError>>,
    operation: F,
) -> io::Result<()>
where
    F: FnOnce() -> Result<Finished, ProcessError> + Send + 'static,
{
    let done = done.clone();
    workers.push(thread::Builder::new().spawn(move || {
        let _ = done.send(operation());
    })?);
    Ok(())
}

/// Run an explicit command. The caller supplies its trusted executable, cwd and environment.
pub fn run_bounded(
    command: Command,
    stdin: Option<Vec<u8>>,
    stdout_limit: usize,
    stderr_limit: usize,
    timeout: Duration,
) -> Result<Output, ProcessError> {
    run_interruptible(command, stdin, stdout_limit, stderr_limit, timeout, || {
        false
    })
}

/// Run a closed subcommand that owns its report limits, streaming its output
/// into the caller's existing file descriptors and applying a process-tree deadline.
pub fn run_streaming(mut command: Command, timeout: Duration) -> Result<ExitStatus, ProcessError> {
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| io::Error::other("timeout is too large"))?;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    let mut group = Contained::spawn(command)?;
    let expired = loop {
        if group.exited()? {
            break false;
        }
        if Instant::now() >= deadline {
            break true;
        }
        thread::sleep(TICK.min(deadline.saturating_duration_since(Instant::now())));
    };
    let status = finish(&mut group, vec![])?;
    if expired {
        Err(ProcessError::Timeout)
    } else {
        Ok(status)
    }
}

/// Run the same contained process while also polling the embedding runtime for
/// cancellation. Every termination path performs the same descendant cleanup.
pub(crate) fn run_interruptible(
    command: Command,
    stdin: Option<Vec<u8>>,
    stdout_limit: usize,
    stderr_limit: usize,
    timeout: Duration,
    interrupted: impl FnMut() -> bool,
) -> Result<Output, ProcessError> {
    run_observed(
        command,
        stdin,
        stdout_limit,
        stderr_limit,
        timeout,
        || {},
        interrupted,
    )
}

/// Notify the caller after starting a contained client so it can distinguish
/// an OS error launching the executable from an error on its pipes.
pub(crate) fn run_observed(
    mut command: Command,
    stdin: Option<Vec<u8>>,
    stdout_limit: usize,
    stderr_limit: usize,
    timeout: Duration,
    started: impl FnOnce(),
    mut interrupted: impl FnMut() -> bool,
) -> Result<Output, ProcessError> {
    if timeout.is_zero() || stdout_limit == 0 || stderr_limit == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "process limits must be positive",
        )
        .into());
    }
    let deadline = Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| io::Error::other("timeout is too large"))?;
    command
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut group = Contained::spawn(command)?;
    started();
    let mut workers = Vec::new();
    let outcome = (|| {
        let stdout = group
            .child
            .stdout
            .take()
            .ok_or_else(|| io::Error::other("missing stdout pipe"))?;
        let stderr = group
            .child
            .stderr
            .take()
            .ok_or_else(|| io::Error::other("missing stderr pipe"))?;
        let (done, results) = mpsc::channel();
        worker(&mut workers, &done, move || {
            capture(stdout, stdout_limit, ProcessError::StdoutLimit).map(Finished::Stdout)
        })?;
        worker(&mut workers, &done, move || {
            capture(stderr, stderr_limit, ProcessError::StderrLimit).map(Finished::Stderr)
        })?;
        if let Some(data) = stdin {
            let mut pipe = group
                .child
                .stdin
                .take()
                .ok_or_else(|| io::Error::other("missing stdin pipe"))?;
            worker(&mut workers, &done, move || {
                if let Err(error) = pipe.write_all(&data)
                    && error.kind() != io::ErrorKind::BrokenPipe
                {
                    return Err(error.into());
                }
                Ok(Finished::Stdin)
            })?;
        }
        drop(done);
        let mut output = (Vec::new(), Vec::new());
        let mut finished = 0;
        loop {
            if interrupted() {
                return Err(io::Error::from(io::ErrorKind::Interrupted).into());
            }
            if Instant::now() >= deadline {
                return Err(ProcessError::Timeout);
            }
            if finished == workers.len() {
                if group.exited()? {
                    return Ok(output);
                }
            } else {
                match results.try_recv() {
                    Ok(event) => {
                        match event? {
                            Finished::Stdout(bytes) => output.0 = bytes,
                            Finished::Stderr(bytes) => output.1 = bytes,
                            Finished::Stdin => {}
                        }
                        finished += 1;
                        continue;
                    }
                    Err(TryRecvError::Disconnected) => {
                        return Err(io::Error::other("process pipe worker failed").into());
                    }
                    Err(TryRecvError::Empty) => {}
                }
            }
            thread::sleep(TICK.min(deadline.saturating_duration_since(Instant::now())));
        }
    })();

    let status = finish(&mut group, workers)?;
    let (stdout, stderr) = outcome?;
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

fn finish(group: &mut Contained, workers: Vec<JoinHandle<()>>) -> Result<ExitStatus, ProcessError> {
    // Kill descendants even when the leader exited cleanly and they closed all pipes.
    let cleanup_deadline = Instant::now() + CLEANUP;
    group.terminate(cleanup_deadline)?;
    let status = loop {
        if workers.iter().all(JoinHandle::is_finished)
            && let Some(status) = group.child.try_wait()?
        {
            break status;
        }
        let remaining = cleanup_deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(io::Error::other("could not clean up the bounded process tree").into());
        }
        thread::sleep(TICK.min(remaining));
    };
    for worker in workers {
        worker
            .join()
            .map_err(|_| io::Error::other("process pipe worker failed"))?;
    }
    Ok(status)
}
