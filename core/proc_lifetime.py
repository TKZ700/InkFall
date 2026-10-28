"""Helper-process lifetime: Windows Job Object + per-platform child guards.

InkFall owns two kinds of helper child processes — the persistent
``ProcessDecoder`` render workers (``rendering.process_decoder``) and the
one-shot ``inkfall-save`` export child (``document.backend``). Their
*graceful* shutdown is handled by the existing terminate/join ladders;
this module covers the other failure mode: a parent that dies abruptly
(crash, hard kill, TerminateProcess) previously left its spawn workers
blocked in ``task_queue.get()`` forever.

- Parent (Windows): :func:`assign_parent_job` puts the whole process
  tree into a Job Object with ``KILL_ON_JOB_CLOSE``. Multiprocessing
  ``spawn`` children inherit job membership automatically, so when the
  parent's process (and with it the job handle) disappears, the kernel
  terminates every owned helper. Assignment failures are swallowed —
  the app must never fail to start over a backstop mechanism.
- Child: :func:`install_child_guard` is called at the top of every child
  entry point. Windows relies on the job; Linux sets ``PDEATHSIG``;
  macOS polls the parent PID. These also cover children created before
  a parent that was never job-assigned.

This module never scans for, or kills, arbitrary processes at startup:
with the job object, InkFall helpers cannot outlive their parent, so
there is nothing to clean up.
"""

from __future__ import annotations

import os
import sys
import threading

__all__ = ["assign_parent_job", "install_child_guard"]

#: Opt-out for tests and exotic environments (job assignment is a
#: backstop, never a requirement).
_ENV_DISABLE = "INKFALL_DISABLE_JOB_GUARD"

_job_assigned = False
_job_lock = threading.Lock()


def assign_parent_job() -> bool:
    """Assign the current process to a kill-on-close Job (Windows).

    Idempotent and failure-tolerant: returns ``True`` when the process
    is job-protected, ``False`` otherwise (non-Windows, disabled via
    environment, or an OS refusal — e.g. a pre-existing job that cannot
    nest). The job handle is deliberately kept open for the process
    lifetime: closing it would kill the tree.
    """
    global _job_assigned
    if _job_assigned:
        return True
    if os.name != "nt" or os.environ.get(_ENV_DISABLE):
        return False
    with _job_lock:
        if _job_assigned:
            return True
        try:
            _job_assigned = _assign_job_windows()
        except Exception:
            _job_assigned = False
        return _job_assigned


def _assign_job_windows() -> bool:
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.windll.kernel32

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", ctypes.wintypes.LARGE_INTEGER),
            ("LimitFlags", ctypes.wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.wintypes.ULONG)),
            ("PriorityClass", ctypes.wintypes.DWORD),
            ("SchedulingClass", ctypes.wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation",
             JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(limits), ctypes.sizeof(limits)):
        return False
    if not kernel32.AssignProcessToJobObject(
            job, kernel32.GetCurrentProcess()):
        return False
    # The handle intentionally stays open until process death: its close
    # is what kills the tree. It is never inherited by children (Python
    # does not mark it inheritable), so only the parent holds it.
    return True


def install_child_guard() -> None:
    """Child-side lifetime guard; call at the top of every child entry.

    Windows: no-op — job membership inherited at spawn covers it. Linux:
    ``PR_SET_PDEATHSIG`` so the kernel signals the child when its parent
    dies (with a getppid race check for a parent that died between spawn
    and this call). Other platforms: a daemon watchdog polling the parent
    PID, the only portable mechanism available there.
    """
    if os.name == "nt":
        return
    parent_pid = os.getppid()
    if parent_pid <= 1:
        return
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            SIGKILL = 9
            if libc.prctl(PR_SET_PDEATHSIG, SIGKILL) == 0:
                # Race: the parent may have died before prctl took effect.
                if os.getppid() != parent_pid:
                    os._exit(0)
                return
        except Exception:
            pass
    _start_ppid_watchdog(parent_pid)


def _start_ppid_watchdog(parent_pid: int) -> None:
    def _watch() -> None:
        import time

        while True:
            time.sleep(1.0)
            if os.getppid() != parent_pid:
                os._exit(0)

    try:
        thread = threading.Thread(
            target=_watch, name="inkfall-parent-watch", daemon=True)
        thread.start()
    except Exception:
        pass
