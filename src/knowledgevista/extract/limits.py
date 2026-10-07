"""Resource limits for the extraction worker, with an explicit answer to "was a limit actually enforced?".

A PDF is untrusted input and PyMuPDF is native code: a hostile or merely huge file can exhaust memory or hang. The
worker therefore runs in its own process, and this module bounds it:

  Windows  a Job Object with a per-process committed-memory limit AND "kill on job close", so the worker dies if it
           exceeds the cap and dies with us if we are killed (no orphaned workers). Measured in
           tests/test_extract_limits.py: a child asked to allocate past the cap fails, and the parent is unharmed.
  POSIX    `RLIMIT_AS` applied in the child before exec. NOT exercised by the Windows-only CI, so it is labelled
           untested in its `note` rather than claimed to work.
  neither  `enforced=False` with the reason. The wall-clock timeout (in client.py) is mandatory everywhere and does
           not depend on this module; a missing memory limit is reported as a warning, never silently assumed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MEMORY_LIMIT_MB = 2048


@dataclass
class Limiter:
    enforced: bool
    mechanism: str
    note: str
    _job: Any = field(default=None, repr=False)

    def close(self) -> None:
        """Release the job. With KILL_ON_JOB_CLOSE this also kills any process still in it."""
        job, self._job = self._job, None
        if job is not None and sys.platform == "win32":
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(job)


def popen_kwargs(memory_limit_mb: int) -> dict[str, Any]:
    """Extra `subprocess.Popen` arguments for platforms that limit a child before it runs (POSIX)."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        import resource
    except ImportError:  # pragma: no cover
        return {}
    limit = memory_limit_mb * 1024 * 1024

    def apply() -> None:  # pragma: no cover - POSIX only, not run by the Windows CI
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return {"preexec_fn": apply}


def attach(process: subprocess.Popen, memory_limit_mb: int) -> Limiter:
    """Bound a running worker. Returns what was and was not enforced; never raises for an unsupported platform."""
    if sys.platform != "win32":
        return Limiter(True, "RLIMIT_AS", "set in the child before exec; not exercised by the Windows-only CI (untested)")
    try:
        return _attach_windows(process, memory_limit_mb)
    except OSError as exc:
        return Limiter(False, "none", f"Job Object could not be applied: {exc}. The wall-clock timeout still applies.")


def _attach_windows(process: subprocess.Popen, memory_limit_mb: int) -> Limiter:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    info.ProcessMemoryLimit = memory_limit_mb * 1024 * 1024
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = ExtendedLimit
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):  # noqa: SLF001
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    return Limiter(True, "windows-job-object", f"per-process committed memory capped at {memory_limit_mb} MB; killed with the parent", job)


def describe_environment() -> dict[str, Any]:
    return {"platform": sys.platform, "pid": os.getpid()}
