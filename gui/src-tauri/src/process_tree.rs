//! Cross-platform process-tree lifecycle management.
//!
//! - On Windows: uses a Job Object with `KILL_ON_JOB_CLOSE` so the embedded backend
//!   and any Playwright/browser child processes terminate cleanly on Stop or host exit.
//! - On Unix (macOS / Linux): spawns each child into its own process group (`setpgid(0, 0)`)
//!   and sends `SIGTERM` followed by `SIGKILL` to `-pgid` on termination.

use std::process::Command;

/// Configures a spawned command to avoid creating a visible console window on Windows.
/// No-op on Unix platforms.
#[cfg(windows)]
pub fn configure_hidden_command(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    cmd.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
pub fn configure_hidden_command(_cmd: &mut Command) {}

/// Configures a command before spawn so its process tree can be terminated as a unit.
/// On Unix this calls `setpgid(0, 0)` in `pre_exec`; on Windows this is a no-op because
/// Job assignment happens right after spawn.
#[cfg(unix)]
pub fn prepare_process_tree_command(cmd: &mut Command) {
    use std::os::unix::process::CommandExt;
    unsafe {
        cmd.pre_exec(|| {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
}

#[cfg(not(unix))]
pub fn prepare_process_tree_command(_cmd: &mut Command) {}

#[cfg(windows)]
mod imp {
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, HANDLE};
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows::Win32::System::Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE};

    pub struct Job(HANDLE);

    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    impl Job {
        pub fn create() -> Result<Self, String> {
            unsafe {
                let handle = CreateJobObjectW(None, PCWSTR::null())
                    .map_err(|e| format!("CreateJobObject failed: {e}"))?;
                let job = Self(handle);
                let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                SetInformationJobObject(
                    job.0,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const core::ffi::c_void,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
                .map_err(|e| format!("SetInformationJobObject failed: {e}"))?;
                Ok(job)
            }
        }

        pub fn assign(&self, pid: u32) -> Result<(), String> {
            unsafe {
                let ph = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, false, pid)
                    .map_err(|e| format!("OpenProcess({pid}) failed: {e}"))?;
                let result = AssignProcessToJobObject(self.0, ph);
                let _ = CloseHandle(ph);
                result.map_err(|e| format!("AssignProcessToJobObject({pid}) failed: {e}"))
            }
        }

        pub fn terminate(&self) {
            unsafe {
                let _ = TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }
}

#[cfg(unix)]
mod imp {
    use std::sync::atomic::{AtomicI32, Ordering};
    use std::thread;
    use std::time::{Duration, Instant};

    pub struct Job {
        pgid: AtomicI32,
    }

    impl Job {
        pub fn create() -> Result<Self, String> {
            Ok(Self {
                pgid: AtomicI32::new(0),
            })
        }

        pub fn assign(&self, pid: u32) -> Result<(), String> {
            let pgid = i32::try_from(pid).map_err(|_| format!("invalid pid: {pid}"))?;
            // Ensure the child is leader of its own process group if pre_exec was skipped.
            unsafe {
                let _ = libc::setpgid(pgid as libc::pid_t, pgid as libc::pid_t);
            }
            self.pgid.store(pgid, Ordering::SeqCst);
            Ok(())
        }

        pub fn terminate(&self) {
            let pgid = self.pgid.swap(0, Ordering::SeqCst);
            if pgid <= 0 {
                return;
            }
            unsafe {
                let _ = libc::kill(-pgid, libc::SIGTERM);
            }
            let deadline = Instant::now() + Duration::from_millis(350);
            while Instant::now() < deadline {
                let alive = unsafe { libc::kill(-pgid, 0) == 0 };
                if !alive {
                    return;
                }
                thread::sleep(Duration::from_millis(30));
            }
            unsafe {
                let _ = libc::kill(-pgid, libc::SIGKILL);
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            self.terminate();
        }
    }
}

pub use imp::Job;
