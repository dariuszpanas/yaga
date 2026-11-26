use std::io;
use std::os::windows::io::{AsRawHandle, BorrowedHandle};
use std::os::windows::process::CommandExt;
use std::process::{Child, Command};
use std::time::Instant;
use win32job::{ExtendedLimitInfo, Job};
use windows_sys::Win32::System::Threading::{CREATE_NO_WINDOW, CREATE_SUSPENDED, ResumeThread};

pub(super) struct Contained {
    pub child: Child,
    job: Job,
}

impl Contained {
    pub fn spawn(mut command: Command) -> io::Result<Self> {
        let mut limits = ExtendedLimitInfo::new();
        limits.limit_kill_on_job_close();
        let job = Job::create_with_limit_info(&limits).map_err(io::Error::other)?;
        command.creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW);
        let mut child = command.spawn()?;
        let activation = job
            .assign_process(child.as_raw_handle() as isize)
            .map_err(io::Error::other)
            .and_then(|()| resume_initial_thread(&child));
        if let Err(error) = activation {
            // Even a failed assignment leaves a suspended process to reap.
            child.kill()?;
            drop(job);
            let deadline = Instant::now() + super::CLEANUP;
            while child.try_wait()?.is_none() {
                if Instant::now() >= deadline {
                    return Err(io::Error::other("could not reap suspended child"));
                }
                std::thread::sleep(super::TICK);
            }
            return Err(error);
        }
        Ok(Self { child, job })
    }

    pub fn exited(&mut self) -> io::Result<bool> {
        Ok(self.child.try_wait()?.is_some())
    }

    #[allow(unsafe_code)]
    pub fn terminate(&mut self, _deadline: Instant) -> io::Result<()> {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;
        // SAFETY: job owns this live handle for the duration of the call.
        if unsafe { TerminateJobObject(self.job.handle() as _, 1) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

// The held primary thread belongs to this exact child and remains suspended until job assignment.
#[rustversion::since(1.100)]
fn resume_initial_thread(child: &Child) -> io::Result<()> {
    use std::os::windows::process::ChildExt;
    resume_thread(child.main_thread_handle())
}

// Older toolchains do not expose the held thread handle. Keep their original
// identity checks and refuse to activate an ambiguous process.
#[rustversion::before(1.100)]
#[allow(unsafe_code)]
fn resume_initial_thread(child: &Child) -> io::Result<()> {
    use std::os::windows::io::{AsHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::Foundation::{ERROR_NO_MORE_FILES, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
    };
    use windows_sys::Win32::System::Threading::{
        GetProcessIdOfThread, OpenThread, THREAD_QUERY_LIMITED_INFORMATION, THREAD_SUSPEND_RESUME,
    };
    let pid = child.id();
    // SAFETY: the snapshot API takes a numeric flag and returns an owned handle.
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: the validated snapshot handle is transferred to exactly one owner.
    let snapshot = unsafe { OwnedHandle::from_raw_handle(snapshot) };
    let mut entry = THREADENTRY32 {
        dwSize: size_of::<THREADENTRY32>() as u32,
        ..Default::default()
    };
    // SAFETY: entry is appropriately sized, initialized and writable.
    if unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut thread_id = None;
    loop {
        if entry.th32OwnerProcessID == pid && thread_id.replace(entry.th32ThreadID).is_some() {
            return Err(io::Error::other(
                "suspended process must contain exactly one initial thread",
            ));
        }
        entry.dwSize = size_of::<THREADENTRY32>() as u32;
        // SAFETY: the snapshot and writable entry remain live throughout iteration.
        if unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } == 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(ERROR_NO_MORE_FILES as i32) {
                return Err(error);
            }
            break;
        }
    }
    let tid =
        thread_id.ok_or_else(|| io::Error::other("suspended process has no initial thread"))?;
    // SAFETY: OpenThread validates the numeric ID and returns an owned handle.
    let handle = unsafe {
        OpenThread(
            THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION,
            0,
            tid,
        )
    };
    if handle.is_null() {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: only this scope owns the newly opened thread handle.
    let handle = unsafe { OwnedHandle::from_raw_handle(handle) };
    // SAFETY: both operations take the open thread handle. Confirm it still
    // belongs to our held process handle before changing its suspend count.
    if unsafe { GetProcessIdOfThread(handle.as_raw_handle()) } != pid {
        return Err(io::Error::other(
            "suspended process thread identity changed",
        ));
    }
    resume_thread(handle.as_handle())
}

#[allow(unsafe_code)]
fn resume_thread(handle: BorrowedHandle<'_>) -> io::Result<()> {
    // SAFETY: the caller holds a live initial-thread handle with resume access.
    match unsafe { ResumeThread(handle.as_raw_handle()) } {
        1 => Ok(()),
        u32::MAX => Err(io::Error::last_os_error()),
        _ => Err(io::Error::other(
            "suspended process thread has an unexpected suspend count",
        )),
    }
}
