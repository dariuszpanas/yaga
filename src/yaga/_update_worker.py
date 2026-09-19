"""Standalone Windows update worker, copied outside the environment before replacement."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


def main() -> int:
    if sys.platform != "win32":
        return 2
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    handles = []
    try:
        for index, raw_pid in enumerate(sys.argv[2:4]):
            handle = kernel.OpenProcess(0x00100000 | 0x1000, False, int(raw_pid))
            if not handle:
                if ctypes.get_last_error() == 87:  # The process already exited.
                    continue
                raise OSError("cannot wait for the YAGA process to exit")
            handles.append(handle)
            if index == 1:
                name = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(name))
                if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                    raise OSError("cannot identify the YAGA launcher")
                if Path(name.value).name.casefold() != "yaga.exe":
                    continue  # python -m yaga has no launcher; never wait for the user's shell.
            if kernel.WaitForSingleObject(handle, 60000) != 0:
                raise OSError("YAGA did not exit within the update handoff timeout")
        result = subprocess.run(
            [sys.argv[1], "--no-config", "tool", "upgrade", "yaga-cli"],
            timeout=600,
            check=False,
        )
        print(f"YAGA_UPDATE_EXIT_CODE={result.returncode}", flush=True)
        return result.returncode
    except (OSError, subprocess.SubprocessError) as error:
        print(
            f"YAGA update failed: {type(error).__name__}; run uv tool upgrade yaga-cli", flush=True
        )
        print("YAGA_UPDATE_EXIT_CODE=2", flush=True)
        return 2
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
        Path(__file__).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
