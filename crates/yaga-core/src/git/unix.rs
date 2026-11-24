use rustix::process::{self, Pid, Signal, WaitId, WaitIdOptions};
use std::io;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command};

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

    pub fn terminate(&mut self) -> io::Result<()> {
        if let Some(group) = self.group {
            match process::kill_process_group(group, Signal::KILL) {
                Ok(()) | Err(rustix::io::Errno::SRCH) => self.group = None,
                // On macOS (XNU), killpg on a process group whose only remaining
                // member is an un-reaped zombie leader returns EPERM instead of
                // ESRCH. When a live descendant still exists in the group, XNU
                // delivers SIGKILL to it and returns Ok(()); check that the
                // leader has already exited before accepting EPERM as complete.
                #[cfg(target_os = "macos")]
                Err(rustix::io::Errno::PERM) if self.exited()? => self.group = None,
                Err(error) => return Err(error.into()),
            }
        }
        Ok(())
    }
}

impl Drop for Contained {
    fn drop(&mut self) {
        let _ = self.terminate();
    }
}
