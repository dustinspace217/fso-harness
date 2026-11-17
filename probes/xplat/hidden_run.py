"""Run one command on a hidden Windows desktop, wait for it, and pass its exit code on.

The Windows design keeps harness sessions out of sight by giving them a desktop of their own
(CreateDesktopW). A process started with that desktop named in its startup information lives
there, and so does everything it starts itself (a child with no desktop named inherits its
parent's). So any probe written as an ordinary script can be measured on a hidden desktop by
starting it through this launcher, with no desktop code in the probe:

    python hidden_run.py --log FILE [--desktop NAME] [--timeout S] -- python some_probe.py ...

The command's stdout and stderr go to --log (a console program started on a hidden desktop
has no console anyone can read). Exit code: the command's own; 5 when it ran past --timeout;
2 when the desktop, the job or the process could not be made.

The command runs inside a Job Object that Windows ends when this launcher exits or times out,
so nothing it started (an editor, the engine) is left running unseen on the hidden desktop
(QA CR-2: the first version ended only the command itself).

Must be started in the logged-in user's session (window station WinSta0): a service session
has no interactive station to make a desktop in.
"""
import argparse
import ctypes
import os
import subprocess
import sys
from pathlib import Path

import win_hidden_desktop as whd      # win32(): the user32/kernel32 signatures, shared with probe 1

STARTF_USESTDHANDLES = 0x00000100
CREATE_SUSPENDED = 0x00000004
WAIT_OBJECT_0, WAIT_TIMEOUT = 0x00000000, 0x00000102
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_EXTENDED_LIMIT_INFORMATION_CLASS = 9      # JobObjectExtendedLimitInformation
EXIT_TIMEOUT, EXIT_SETUP = 5, 2
RESUME_FAILED = 0xFFFFFFFF                    # ResumeThread's (DWORD)-1
DEFAULT_DESKTOP = "fsoharness-hidden"
DEFAULT_TIMEOUT_S = 900


def exit_code_for(wait_result: int, child_code: int | None) -> int:
    """The launcher's exit code from WaitForSingleObject's result and the child's own code:
    the child's code when it ended in time, 5 on timeout, 2 for anything else (a failed wait,
    or an exit code that could not be read)."""
    if wait_result == WAIT_TIMEOUT:
        return EXIT_TIMEOUT
    if wait_result != WAIT_OBJECT_0 or child_code is None:
        return EXIT_SETUP
    return child_code


def job_api():
    """kernel32's Job Object and process calls with their types (handles are pointer-sized)."""
    from ctypes import wintypes as wt  # noqa: PLC0415 - Windows-only

    class BASIC(ctypes.Structure):               # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wt.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wt.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

    class EXTENDED(ctypes.Structure):            # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    for fn, res, args in (
            (k.CreateJobObjectW, wt.HANDLE, [ctypes.c_void_p, wt.LPCWSTR]),
            (k.SetInformationJobObject, wt.BOOL, [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wt.DWORD]),
            (k.AssignProcessToJobObject, wt.BOOL, [wt.HANDLE, wt.HANDLE]),
            (k.ResumeThread, wt.DWORD, [wt.HANDLE]),
            (k.TerminateProcess, wt.BOOL, [wt.HANDLE, wt.UINT]),
            (k.CloseHandle, wt.BOOL, [wt.HANDLE]),
            (k.GetExitCodeProcess, wt.BOOL, [wt.HANDLE, ctypes.POINTER(wt.DWORD)])):
        fn.restype, fn.argtypes = res, args
    return k, EXTENDED


def make_job(k, extended):
    """A Job Object whose processes Windows ends when its last handle closes (this launcher
    holds the only one, so they end when it exits, however it exits)."""
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = extended()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_INFORMATION_CLASS,
                                     ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.WinError(ctypes.get_last_error())
        k.CloseHandle(job)            # a job without the limit is of no use: do not leak it
        raise error
    return job


def release_into_job(k, job, proc, thread) -> None:
    """Put the suspended process in the job and let it run. WHY the failure path ends the
    process (QA SC-4): a child that could not be put in the job is suspended, outside the job
    and on a desktop nobody sees; leaving it would be the leftover the job exists to prevent.
    A failed resume is the same case. Closes the thread handle either way; raises OSError."""
    try:
        if not k.AssignProcessToJobObject(job, proc):
            raise ctypes.WinError(ctypes.get_last_error())
        if k.ResumeThread(thread) == RESUME_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
    except OSError:
        k.TerminateProcess(proc, EXIT_SETUP)
        k.CloseHandle(proc)
        raise
    finally:
        k.CloseHandle(thread)


def start_on_desktop(w, command: list, desktop: str, log_handle: int):
    """Start command SUSPENDED on desktop ("station\\desktop") with stdout and stderr sent to
    the open file log_handle; return (process handle, thread handle). Suspended so it can be
    put in the job before it runs or starts a child. Raises OSError on failure."""
    si = w.STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop
    # WHY handles are passed explicitly: with STARTF_USESTDHANDLES the child writes to the log
    # file instead of a console window nobody can see. bInheritHandles must then be True, and
    # the handle itself must be marked inheritable (os.set_handle_inheritable in run()).
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdOutput = si.hStdError = log_handle
    pi = w.PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    if not w.k.CreateProcessW(None, cmdline, None, None, True, CREATE_SUSPENDED, None, None,
                              ctypes.byref(si), ctypes.byref(pi)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pi.hProcess, pi.hThread


def run(args, command: list) -> int:
    """Make the desktop and the job, run the command in both, wait; return the exit code."""
    import msvcrt  # noqa: PLC0415 - Windows-only; the unit tier imports this file on Linux
    w = whd.win32()
    k, extended = job_api()
    hdesk = w.u.CreateDesktopW(args.desktop, None, None, 0, whd.GENERIC_ALL, None)
    if not hdesk:
        print(f"CreateDesktopW error {ctypes.get_last_error()}", file=sys.stderr)
        return EXIT_SETUP
    target = f"{whd.station_name(w)}\\{args.desktop}"
    job = None
    try:
        job = make_job(k, extended)
        with open(args.log, "wb") as log:
            handle = msvcrt.get_osfhandle(log.fileno())
            os.set_handle_inheritable(handle, True)
            proc, thread = start_on_desktop(w, command, target, handle)
        release_into_job(k, job, proc, thread)
        waited = w.k.WaitForSingleObject(proc, int(args.timeout * 1000))
        code = ctypes.c_ulong()
        got = k.GetExitCodeProcess(proc, ctypes.byref(code)) if waited == WAIT_OBJECT_0 else 0
        w.k.CloseHandle(proc)
        if waited == WAIT_TIMEOUT:
            print(f"timed out after {args.timeout} s; ending everything it started",
                  file=sys.stderr)
        return exit_code_for(waited, int(code.value) if got else None)
    except OSError as e:
        print(f"hidden_run: {e}", file=sys.stderr)
        return EXIT_SETUP
    finally:
        if job:
            w.k.CloseHandle(job)      # ends every process still in the job
        # The desktop goes away when its last handle closes and no process is left on it.
        w.u.CloseDesktop(hdesk)


def main() -> int:
    """Parse the command line and run the command on a hidden desktop."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--desktop", default=DEFAULT_DESKTOP)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("no command given after --")
    return run(args, command)


if __name__ == "__main__":
    sys.exit(main())
