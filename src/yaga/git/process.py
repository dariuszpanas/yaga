"""Standard-library process-tree containment for bounded Git commands."""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Protocol


class ProcessTree(Protocol):
    """One operating-system process-tree containment boundary."""

    def creation_flags(self) -> int:
        """Return platform-specific process creation flags."""

    def starts_new_session(self) -> bool:
        """Return whether the process must start a new POSIX session."""

    def attach_and_start(self, process: subprocess.Popen[bytes]) -> None:
        """Attach a newly created process and allow it to run."""

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        """Terminate every process still contained in the tree."""

    def close(self) -> None:
        """Close containment, terminating any remaining contained processes."""


class _PosixProcessTree:
    def __init__(self) -> None:
        self._process_group_id: int | None = None

    def creation_flags(self) -> int:
        return 0

    def starts_new_session(self) -> bool:
        return True

    def attach_and_start(self, process: subprocess.Popen[bytes]) -> None:
        self._process_group_id = process.pid

    def terminate(self, process: subprocess.Popen[bytes]) -> None:
        self._kill_process_group(process)

    def close(self) -> None:
        process_group_id = self._process_group_id
        if process_group_id is None:
            return
        self._kill_process_group(None, process_group_id=process_group_id)
        self._process_group_id = None

    def _kill_process_group(
        self,
        process: subprocess.Popen[bytes] | None,
        *,
        process_group_id: int | None = None,
    ) -> None:
        kill_process_group = getattr(os, "killpg", None)
        kill_signal = getattr(signal, "SIGKILL", None)
        if not callable(kill_process_group) or not isinstance(kill_signal, int):
            raise OSError("process-group termination is unavailable")
        selected_group = (
            process_group_id
            or self._process_group_id
            or (process.pid if process is not None else None)
        )
        if selected_group is None:
            return
        try:
            kill_process_group(selected_group, kill_signal)
        except ProcessLookupError:
            if process is not None and process.poll() is None:
                process.kill()


if os.name == "nt":  # pragma: win32 cover
    import ctypes
    from ctypes import wintypes

    _CREATE_SUSPENDED = 0x00000004
    _ERROR_NO_MORE_FILES = 18
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _RESUME_FAILED = 0xFFFFFFFF

    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        )

    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        )

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _create_job_object = _kernel32.CreateJobObjectW
    _create_job_object.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _create_job_object.restype = wintypes.HANDLE

    _set_information_job_object = _kernel32.SetInformationJobObject
    _set_information_job_object.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _set_information_job_object.restype = wintypes.BOOL

    _assign_process_to_job_object = _kernel32.AssignProcessToJobObject
    _assign_process_to_job_object.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _assign_process_to_job_object.restype = wintypes.BOOL

    _terminate_job_object = _kernel32.TerminateJobObject
    _terminate_job_object.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _terminate_job_object.restype = wintypes.BOOL

    _open_process = _kernel32.OpenProcess
    _open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _open_process.restype = wintypes.HANDLE

    _create_thread_snapshot = _kernel32.CreateToolhelp32Snapshot
    _create_thread_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _create_thread_snapshot.restype = wintypes.HANDLE

    _thread32_first = _kernel32.Thread32First
    _thread32_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    _thread32_first.restype = wintypes.BOOL

    _thread32_next = _kernel32.Thread32Next
    _thread32_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
    _thread32_next.restype = wintypes.BOOL

    _open_thread = _kernel32.OpenThread
    _open_thread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _open_thread.restype = wintypes.HANDLE

    _resume_thread = _kernel32.ResumeThread
    _resume_thread.argtypes = (wintypes.HANDLE,)
    _resume_thread.restype = wintypes.DWORD

    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL

    def _windows_error(operation: str) -> OSError:
        return ctypes.WinError(ctypes.get_last_error(), operation)

    def _close_windows_handle(handle: int, *, label: str) -> None:
        if not _close_handle(handle):
            raise _windows_error(f"could not close {label}")

    class _WindowsProcessTree:
        def __init__(self) -> None:
            handle = _create_job_object(None, None)
            if not handle:
                raise _windows_error("could not create process job")
            self._handle: int | None = handle
            information = _JobExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _set_information_job_object(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                error = _windows_error("could not configure process job")
                try:
                    _close_windows_handle(handle, label="process job")
                except OSError:
                    pass
                self._handle = None
                raise error

        def creation_flags(self) -> int:
            return _CREATE_SUSPENDED

        def starts_new_session(self) -> bool:
            return False

        def attach_and_start(self, process: subprocess.Popen[bytes]) -> None:
            handle = self._require_handle()
            process_handle = _open_process(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                False,
                process.pid,
            )
            if not process_handle:
                raise _windows_error("could not open suspended process")
            try:
                if not _assign_process_to_job_object(handle, process_handle):
                    raise _windows_error("could not assign process to job")
            finally:
                _close_windows_handle(process_handle, label="process handle")
            self._resume_initial_thread(process.pid)

        def terminate(self, process: subprocess.Popen[bytes]) -> None:
            del process
            if not _terminate_job_object(self._require_handle(), 1):
                raise _windows_error("could not terminate process job")

        def close(self) -> None:
            handle = self._handle
            if handle is None:
                return
            _close_windows_handle(handle, label="process job")
            self._handle = None

        def _require_handle(self) -> int:
            if self._handle is None:
                raise OSError("process job is already closed")
            return self._handle

        def _resume_initial_thread(self, process_id: int) -> None:
            snapshot = _create_thread_snapshot(_TH32CS_SNAPTHREAD, 0)
            if snapshot == _INVALID_HANDLE_VALUE:
                raise _windows_error("could not enumerate suspended process threads")
            thread_ids: list[int] = []
            try:
                entry = _ThreadEntry32()
                entry.dwSize = ctypes.sizeof(entry)
                if not _thread32_first(snapshot, ctypes.byref(entry)):
                    raise _windows_error("could not enumerate suspended process threads")
                while True:
                    if entry.th32OwnerProcessID == process_id:
                        thread_ids.append(entry.th32ThreadID)
                    entry.dwSize = ctypes.sizeof(entry)
                    ctypes.set_last_error(0)
                    if not _thread32_next(snapshot, ctypes.byref(entry)):
                        error_code = ctypes.get_last_error()
                        if error_code not in {0, _ERROR_NO_MORE_FILES}:
                            raise ctypes.WinError(
                                error_code,
                                "could not enumerate suspended process threads",
                            )
                        break
            finally:
                _close_windows_handle(snapshot, label="thread snapshot")
            if len(thread_ids) != 1:
                raise OSError("suspended process must contain exactly one initial thread")
            self._resume_one_thread(thread_ids[0])

        @staticmethod
        def _resume_one_thread(thread_id: int) -> None:
            thread = _open_thread(_THREAD_SUSPEND_RESUME, False, thread_id)
            if not thread:
                raise _windows_error("could not open suspended process thread")
            try:
                previous_count = _resume_thread(thread)
                if previous_count == _RESUME_FAILED:
                    raise _windows_error("could not resume suspended process thread")
                if previous_count != 1:
                    raise OSError("suspended process thread has an unexpected suspend count")
            finally:
                _close_windows_handle(thread, label="process thread")


def open_process_tree() -> ProcessTree:
    """Create one platform-native process-tree containment boundary."""
    if os.name == "nt":  # pragma: win32 cover
        return _WindowsProcessTree()
    return _PosixProcessTree()
