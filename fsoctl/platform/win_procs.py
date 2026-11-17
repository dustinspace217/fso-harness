"""Facts about processes on Windows (the twin of linux_procs.py): Win32 process handles, Job
Object membership, `netstat`, and the module list.

Every function opens what it needs and closes it before returning: a handle left open keeps
an ended process's record alive in Windows, and this code runs in short-lived fsoctl calls
and in the long-lived runner alike.
"""
import ctypes
import ntpath
import os
import subprocess
from collections.abc import Callable

from . import win_api

MAX_MODULES = 2048        # module handles asked for at once; an engine loads a few hundred
PATH_CHARS = 32768        # the longest path Windows can return


def _open(pid: int, access: int):
    """A handle to process pid with `access`, or None when there is no such process (or it
    is not ours to open)."""
    return win_api.kernel32().OpenProcess(access, False, pid) or None


def pid_alive(pid: int) -> bool:
    """True while process pid has not ended.

    WHY a zero-length wait and not os.kill(pid, 0): on Windows os.kill with signal 0 is
    TerminateProcess, it ends the process. And WHY not "exit code is STILL_ACTIVE (259)":
    a process may exit with code 259, and would then read as running for ever. A process
    handle becomes signalled exactly when the process ends, so waiting on it for 0 ms asks
    the question itself. An ended process can still be opened while anyone holds a handle to
    it; the wait answers correctly for that too."""
    k = win_api.kernel32()
    handle = _open(pid, win_api.SYNCHRONIZE | win_api.PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        return False
    try:
        return k.WaitForSingleObject(handle, 0) == win_api.WAIT_TIMEOUT
    finally:
        k.CloseHandle(handle)


def pid_ended(pid: int) -> bool:
    """True only when it is certain that no running process has this pid: Windows says there
    is none (OpenProcess fails with "invalid parameter"; tests/winlive/test_procs_live.py
    holds real Windows to that), or the process that has it has ended (its handle is
    signalled). A process that cannot be opened for another reason, or
    a wait that fails, is "not known" and answers False.

    WHY beside pid_alive, which answers "not alive" for all of these (QA Plan B, CR-1): code
    that ends something because its owner is gone must act on a finding, never on a lookup
    that failed."""
    k = win_api.kernel32()
    handle = k.OpenProcess(win_api.SYNCHRONIZE | win_api.PROCESS_QUERY_LIMITED_INFORMATION,
                           False, pid)
    if not handle:
        return win_api.last_error() == win_api.ERROR_INVALID_PARAMETER
    try:
        return k.WaitForSingleObject(handle, 0) == win_api.WAIT_OBJECT_0
    finally:
        k.CloseHandle(handle)


def pid_start(pid: int) -> int | None:
    """The process's creation time as one number (FILETIME: 100 ns units since 1601), or None
    if it is gone. Two processes that reuse a pid differ in it, which is all callers need."""
    k = win_api.kernel32()
    handle = _open(pid, win_api.PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        return None
    try:
        created, ended, kernel, user = (win_api.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended),
                                 ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return (created.high << 32) | created.low
    finally:
        k.CloseHandle(handle)


def in_group(pid: int, unit: str) -> bool:
    """True when process pid exists and is in the Job Object named `unit` (a session's job
    has its scope name: win_scope). A job that cannot be opened answers False. Usually it no
    longer exists; from another Windows login, or without the right to open it, a live job
    answers False too (win_scope._may_be_there), which is why the launcher also asks about
    the recorded runner (launcher._gone)."""
    k = win_api.kernel32()
    job = k.OpenJobObjectW(win_api.JOB_OBJECT_QUERY, False, unit) or None
    if job is None:
        return False
    try:
        handle = _open(pid, win_api.PROCESS_QUERY_LIMITED_INFORMATION)
        if handle is None:
            return False
        try:
            inside = win_api.BOOL(0)
            return bool(k.IsProcessInJob(handle, job, ctypes.byref(inside))) and bool(inside.value)
        finally:
            k.CloseHandle(handle)
    finally:
        k.CloseHandle(job)


def gone(pid: int) -> bool:
    """True when pid no longer runs. Windows has no zombie state to tell apart."""
    return not pid_alive(pid)


def ask_stop(sess) -> str | None:
    """Ask the session's runner to end the engine and exit: leave the stop marker
    (session.STOP_MARKER) in the run folder, which the runner looks for twice a second
    (win_runner). `fsoctl stop` leaves the same file itself before it does anything else,
    so it is made here only if it is not there yet (round-2 CRF-5: a second write that
    failed on a busy file answered "skipped", and the group was ended by force under a
    runner already closing the engine). WHY a file and not a signal:
    Windows has nothing like SIGTERM for a process without a console; the only other way to
    reach the runner is to end it outright, which would skip its orderly shutdown of the
    engine. Returns None (asked), or "skipped:<why>" when the file cannot be made, as the
    Linux twin does when it cannot signal: the caller then stops the process group itself."""
    from .. import paths, session  # noqa: PLC0415 - both import this package; importing
    # them here, at call time, keeps that from being a circle at import time
    marker = paths.session_dir(sess.id) / session.STOP_MARKER
    if marker.exists():
        return None
    try:
        marker.touch()
    except OSError as e:
        return f"skipped:{e.strerror or e}"
    return None


def _host(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a short query on the host, bounded, text output."""
    return subprocess.run(argv, capture_output=True, text=True, errors="replace",
                          timeout=10, check=False)


def _netstat() -> str:
    """netstat by its full path in the Windows folder. WHY not the bare name (QA Plan B,
    SA-1): Windows looks for a bare name in the current folder first, and this runs at
    every launch, from whatever folder the command was typed in."""
    return ntpath.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "netstat.exe")


def socket_owner_pids(proto: str, port: int,
                      run: Callable[[list[str]], subprocess.CompletedProcess] | None = None
                      ) -> set[int]:
    """The pids that hold a listening `proto` ("udp" | "tcp") socket on `port`, from
    `netstat -ano` (-o adds the owning pid as the last column). A row counts when its first
    column is the protocol, the text after the last ":" of its local address is the port (so
    7821 does not match 78210, and "[::]:7821" matches) and, for TCP, its state is LISTENING;
    UDP rows have no state column. `run` is the Linux twin's hook for running the query
    inside a private match's network; Windows has no such networks yet, so it is only used
    when given.

    WHY the whole listing and not `-p UDP`: that form lists IPv4 sockets only (measured in
    the VM, 2026-11-16: a port held on [::1] by an IPv6-only socket was missing from it and
    present in `-p UDPv6` and in the plain listing). The engine's game socket is IPv6 dual
    stack, so a holder in either family keeps it from binding, and the launcher's port test
    on Windows is this lookup."""
    r = (run or _host)([_netstat(), "-ano"])
    if r.returncode != 0:
        return set()
    owners = set()
    for line in r.stdout.splitlines():
        cols = line.split()
        if len(cols) < 4 or cols[0] != proto.upper() or not cols[-1].isdigit():
            continue
        if cols[1].rsplit(":", 1)[-1] != str(port):
            continue
        if proto == "tcp" and "LISTENING" not in cols:
            continue
        owners.add(int(cols[-1]))
    return owners


def loaded_libraries(pid: int) -> str:
    """The file names of the modules process pid has loaded, one per line, or "" if the
    process is gone or cannot be read. renderer.classify reads it, as it reads
    /proc/<pid>/maps on Linux."""
    k, p = win_api.kernel32(), win_api.psapi()
    handle = _open(pid, win_api.PROCESS_QUERY_INFORMATION | win_api.PROCESS_VM_READ)
    if handle is None:
        return ""
    try:
        modules = (win_api.HANDLE * MAX_MODULES)()
        needed = win_api.DWORD(0)
        if not p.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules),
                                      ctypes.byref(needed), win_api.LIST_MODULES_ALL):
            return ""
        count = min(needed.value // ctypes.sizeof(win_api.HANDLE), MAX_MODULES)
        name = ctypes.create_unicode_buffer(PATH_CHARS)
        names = []
        for i in range(count):                    # bounded by MAX_MODULES
            if p.GetModuleFileNameExW(handle, modules[i], name, PATH_CHARS):
                names.append(name.value)
        return "\n".join(names)
    finally:
        k.CloseHandle(handle)
