use rustix::process::{self, Pid, Signal, WaitId, WaitIdOptions};
use std::io;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command};
use std::time::Instant;

pub(super) struct Contained {
    pub child: Child,
    group: Option<Pid>,
}

impl Contained {
    #[allow(unsafe_code)]
    pub fn spawn(mut command: Command) -> io::Result<Self> {
        // SAFETY: setsid is async-signal-safe; the pre-exec closure does not
        // allocate, lock, or call code inherited from application threads.
        unsafe {
            command.pre_exec(|| process::setsid().map(|_| ()).map_err(io::Error::from));
        }
        let child = command.spawn()?;
        let group = Some(Pid::from_child(&child));
        Ok(Self { child, group })
    }

    pub fn exited(&mut self) -> io::Result<bool> {
        // Keep the group leader waitable until after group termination. Reaping
        // it earlier would permit its PID to be reused for an unrelated group.
        let options = WaitIdOptions::EXITED | WaitIdOptions::NOHANG | WaitIdOptions::NOWAIT;
        process::waitid(WaitId::Pid(Pid::from_child(&self.child)), options)
            .map(|status| status.is_some())
            .map_err(Into::into)
    }

    pub fn terminate(&mut self, deadline: Instant) -> io::Result<()> {
        let Some(group) = self.group else {
            return Ok(());
        };
        #[cfg(target_os = "macos")]
        {
            // On macOS (XNU), killpg on a process group whose remaining members
            // are exiting (P_LEXIT) or un-reaped zombies (SZOMB) returns EPERM
            // instead of ESRCH. When a live descendant still exists in the
            // group, XNU delivers SIGKILL to it and returns Ok(()). While the
            // leader is in P_LEXIT, non-blocking waitid(WEXITED | WNOHANG |
            // WNOWAIT) can still return false until it transitions to SZOMB, so
            // retry until the bounded cleanup deadline while keeping genuine
            // permanent EPERM failures fail-closed.
            super::terminate_macos_group(
                deadline,
                super::TICK,
                Instant::now,
                || match process::kill_process_group(group, Signal::KILL) {
                    Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(super::GroupKill::Done),
                    Err(rustix::io::Errno::PERM) => Ok(super::GroupKill::PermissionDenied(
                        rustix::io::Errno::PERM.into(),
                    )),
                    Err(error) => Err(error.into()),
                },
                || self.exited(),
                std::thread::sleep,
            )?;
            self.group = None;
            Ok(())
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = deadline;
            match process::kill_process_group(group, Signal::KILL) {
                Ok(()) | Err(rustix::io::Errno::SRCH) => {
                    self.group = None;
                    Ok(())
                }
                Err(error) => Err(error.into()),
            }
        }
    }
}

impl Drop for Contained {
    fn drop(&mut self) {
        let _ = self.terminate(Instant::now());
    }
}
