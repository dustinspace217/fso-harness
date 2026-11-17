"""Named Job Objects: the resource ceiling and the kill switch for every run on Windows.
The twin of scope.py (same public functions; tests/test_platform_parity.py holds it to that).

A Job Object is Windows' group of processes: limits set on the job apply to everything in it,
and ending the job ends them all, children included. A session's job is named after its
scope (fso-harness-<id>), so a later `fsoctl stop` or `status`, a different process that has
only the name, can find it.

WHY the first process INHERITS a handle to its own job (probe 10, 2026-11-16,
docs/research/xplat/probe-win-job-name.json): Windows keeps a job alive while processes are
in it, but removes its NAME as soon as the last handle to it closes. `fsoctl launch` exits a
moment after starting the session; with only its handle, the name was gone while the session
ran (OpenJobObjectW: error 2) and nothing could stop the session by name. A handle held by a
process inside the job keeps the name. So the job handle is made inheritable before the first
process starts, and the runner starts the engine with handle inheritance too: the name lasts
as long as the runner or the engine lives.
What follows from that, and is accepted: if both are gone and some stray grandchild is not,
the job can no longer be found by name; is_active says False for it.

WHY NOT kill-on-close: that flag ends every process when the last handle closes, which is
right for a wrapper that outlives its children (probes/xplat/hidden_run.py) and wrong here,
where the launching command exits first by design. The one wrapper in this module that does
outlive its children is run(), the wait-for-it form `fsoctl build` uses, and its job has the
flag.

What Windows has no word for: MemorySwapMax (a job's memory limit counts committed memory,
swap included) and RuntimeMaxSec (the runner ends the run itself: win_runner). Both are
accepted in props and not turned into a limit.

One property exists only here: "Desktop=<name>" makes a desktop of that name and starts the
first process on it (win_desktop); without it the process starts on the user's desktop. WHY a
property and not a parameter of start(): the launcher already hands start() a list of
"Key=Value" strings that each OS reads in its own way, so the Linux start() keeps its
signature and never sees the word. The first process inherits a handle to the desktop for
the reason it inherits the job handle: a desktop is gone once nothing holds it.
"""
import ctypes
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import EXIT_ENGINE, EXIT_LAUNCH, HarnessError
from . import win_api, win_desktop

_MEM_FLOOR_KB = 2 * 1024 * 1024
_MEM_SHARE = 0.60
_MEM_RESERVE_KB = 8 * 1024 * 1024   # left for the desktop whatever the share says
_KILL_WAIT = 10.0     # seconds to wait for an ended job to empty
_RUN_POLL_MS = 200    # run(): how long one wait on the program lasts
POLITE_STOP_SEC = 20  # stop()'s default wait (the name scope.py's callers use)
_WHOLE_MACHINE = 10000   # CpuRate for 100% of all processors


@dataclass
class Started:
    """What start() returns: the first process of the job. scope.py returns a Popen; callers
    use only its pid."""
    pid: int


def _mem_available_kb() -> int:
    """Physical memory free to use right now, in KiB (GlobalMemoryStatusEx)."""
    status = win_api.MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not win_api.kernel32().GlobalMemoryStatusEx(ctypes.byref(status)):
        raise HarnessError(EXIT_LAUNCH, "cannot read the machine's available memory "
                           f"(GlobalMemoryStatusEx error {win_api.last_error()})",
                           "this is unexpected; run `fsoctl launch` again")
    return status.ullAvailPhys // 1024


def mem_cap_kb() -> int:
    """Return the memory ceiling a new job gets: the share of available memory, never into
    the reserve (the same rule as scope.py)."""
    avail = _mem_available_kb()
    return max(_MEM_FLOOR_KB, min(int(avail * _MEM_SHARE), avail - _MEM_RESERVE_KB))


def room_for_engine() -> str | None:
    """Return why a new engine must not start now, or None when the machine has room (the
    same test as scope.py: available memory minus the desktop's reserve must reach the floor)."""
    avail = _mem_available_kb()
    if avail - _MEM_RESERVE_KB < _MEM_FLOOR_KB:
        return (f"only {avail // 1048576} GB available with {_MEM_RESERVE_KB // 1048576} GB "
                "reserved for the desktop")
    return None


def caps(runtime_sec: int = 1800, cpu_cores: int = 8, tasks: int = 512,
         mem_kb: int | None = None) -> list[str]:
    """Return the run's limits as the same "Key=Value" strings scope.py produces (the
    launcher reads MemoryMax= out of them); start() turns them into job limits."""
    if mem_kb is None:
        mem_kb = mem_cap_kb()
    return [f"MemoryMax={mem_kb}K", "MemorySwapMax=0", f"CPUQuota={cpu_cores * 100}%",
            f"TasksMax={tasks}", f"RuntimeMaxSec={runtime_sec}"]


def _limits(props: list[str]) -> dict:
    """Read caps() strings into {"memory": bytes, "processes": count, "cpu_rate": rate,
    "desktop": name}, each None when not asked for. cpu_rate is hundredths of a percent of
    all processors, None when the quota is the whole machine or more (nothing to cap).
    run() adds "kill_on_close": True to the result; no property asks for it."""
    found = dict(p.split("=", 1) for p in props)
    memory = processes = cpu_rate = None
    desktop = found.get("Desktop")
    if "MemoryMax" in found:
        memory = int(found["MemoryMax"].removesuffix("K")) * 1024
    if "TasksMax" in found:
        processes = int(found["TasksMax"])
    if "CPUQuota" in found:
        percent_of_one_core = int(found["CPUQuota"].removesuffix("%"))
        rate = percent_of_one_core * 100 // (os.cpu_count() or 1)
        cpu_rate = rate if 0 < rate < _WHOLE_MACHINE else None
    return {"memory": memory, "processes": processes, "cpu_rate": cpu_rate,
            "desktop": desktop}


def _apply_limits(k, job, limits: dict) -> None:
    """Set the job's memory, process-count and CPU limits, and kill-on-close when asked
    (limits["kill_on_close"], run() only); raise OSError if Windows refuses."""
    kill_on_close = limits.get("kill_on_close", False)
    if limits["memory"] is not None or limits["processes"] is not None or kill_on_close:
        info = win_api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if kill_on_close:
            info.BasicLimitInformation.LimitFlags |= win_api.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if limits["memory"] is not None:
            info.BasicLimitInformation.LimitFlags |= win_api.JOB_OBJECT_LIMIT_JOB_MEMORY
            info.JobMemoryLimit = limits["memory"]
        if limits["processes"] is not None:
            info.BasicLimitInformation.LimitFlags |= win_api.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = limits["processes"]
        if not k.SetInformationJobObject(job, win_api.JOB_EXTENDED_LIMIT_INFORMATION,
                                         ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(f"SetInformationJobObject (limits) error {win_api.last_error()}")
    if limits["cpu_rate"] is not None:
        cpu = win_api.JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        cpu.ControlFlags = (win_api.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                            | win_api.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP)
        cpu.CpuRate = limits["cpu_rate"]
        if not k.SetInformationJobObject(job, win_api.JOB_CPU_RATE_CONTROL_INFORMATION,
                                         ctypes.byref(cpu), ctypes.sizeof(cpu)):
            raise OSError(f"SetInformationJobObject (CPU) error {win_api.last_error()}")


def _make_job(k, unit: str, limits: dict):
    """Create the job named `unit` with its limits and an inheritable handle; return the
    handle. Raises OSError; a job of that name already existing is an error (session ids are
    random; a second job of the same name would mean two sessions sharing one)."""
    # "Already exists" is read below after a call that succeeded, so nothing older may be
    # left in the error slot.
    win_api.clear_last_error()
    job = k.CreateJobObjectW(None, unit)
    if not job:
        raise OSError(f"CreateJobObjectW error {win_api.last_error()}")
    try:
        if win_api.last_error() == win_api.ERROR_ALREADY_EXISTS:
            raise OSError(f"a job named {unit} already exists")
        _apply_limits(k, job, limits)
        if not k.SetHandleInformation(job, win_api.HANDLE_FLAG_INHERIT,
                                      win_api.HANDLE_FLAG_INHERIT):
            raise OSError(f"SetHandleInformation error {win_api.last_error()}")
    except OSError:
        k.CloseHandle(job)
        raise
    return job


def _environment_block(env: dict) -> ctypes.Array:
    """The environment as CreateProcessW takes it: "NAME=value" strings, each ended by a NUL,
    the whole ended by one more; sorted by name without regard to case, as Windows expects."""
    lines = [f"{name}={value}" for name, value in sorted(env.items(),
                                                        key=lambda item: item[0].upper())]
    return ctypes.create_unicode_buffer("\0".join(lines) + "\0")


def _inheritable_handle(f) -> int:
    """The Windows handle of open file f, marked so a child process inherits it."""
    import msvcrt  # noqa: PLC0415 - Windows-only module
    handle = msvcrt.get_osfhandle(f.fileno())
    os.set_handle_inheritable(handle, True)
    return handle


def _handle_list(k, handles: list[int]):
    """An attribute list saying "the new process inherits exactly these handles"; returns
    (the list's buffer, the handle array). Both must stay alive until CreateProcessW has
    returned, and the buffer goes to DeleteProcThreadAttributeList afterwards.

    WHY (found on real Windows, 2026-11-16): bInheritHandles alone hands the child EVERY
    inheritable handle of this process, and that includes this command's own stdout when a
    caller is capturing it. The session's first process then held the caller's pipe open, and
    the caller (the MCP server reading `fsoctl launch`) waited until the session ended."""
    size = win_api.SIZE_T(0)
    # The first call only reports the size needed (and returns failure by design).
    _ = k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not k.InitializeProcThreadAttributeList(buffer, 1, 0, ctypes.byref(size)):
        raise OSError(f"InitializeProcThreadAttributeList error {win_api.last_error()}")
    array = (win_api.HANDLE * len(handles))(*handles)
    if not k.UpdateProcThreadAttribute(buffer, 0, win_api.PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                       array, ctypes.sizeof(array), None, None):
        k.DeleteProcThreadAttributeList(buffer)
        raise OSError(f"UpdateProcThreadAttribute error {win_api.last_error()}")
    return buffer, array


def _spawn(k, argv: list[str], env: dict, desktop: str | None, inherit: list[int]):
    """Start argv SUSPENDED with its output in the log; return (process handle, thread handle,
    pid). Suspended so it can be put in the job before it runs or starts a child.

    `inherit` lists every handle the child gets and it gets no others (_handle_list): the
    log's, as its stdout and stderr; for a session also the job's, to keep the job's name
    alive (module docstring), and the session desktop's when it has one. A build passes the
    log's alone. CREATE_NO_WINDOW: the runner is a console program and must not
    open a console window. lpDesktop None means the user's own desktop (a --visible run)."""
    startup = win_api.STARTUPINFOEXW()
    si = startup.StartupInfo
    si.cb = ctypes.sizeof(startup)
    si.lpDesktop = desktop
    si.dwFlags = win_api.STARTF_USESTDHANDLES
    si.hStdInput = None
    si.hStdOutput = si.hStdError = inherit[0]
    pi = win_api.PROCESS_INFORMATION()
    flags = (win_api.CREATE_SUSPENDED | win_api.CREATE_NEW_PROCESS_GROUP
             | win_api.CREATE_NO_WINDOW | win_api.CREATE_UNICODE_ENVIRONMENT
             | win_api.EXTENDED_STARTUPINFO_PRESENT)
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    attributes, _kept_alive = _handle_list(k, inherit)
    startup.lpAttributeList = ctypes.addressof(attributes)
    try:
        if not k.CreateProcessW(None, cmdline, None, None, True, flags, _environment_block(env),
                                None, ctypes.byref(si), ctypes.byref(pi)):
            raise OSError(f"CreateProcessW error {win_api.last_error()}")
    finally:
        k.DeleteProcThreadAttributeList(attributes)
    return pi.hProcess, pi.hThread, pi.dwProcessId


def _inheritable_desktop(k, name: str):
    """Create the desktop `name` and return a handle to it that a child process inherits."""
    handle = win_desktop.create(name)
    if not k.SetHandleInformation(handle, win_api.HANDLE_FLAG_INHERIT,
                                  win_api.HANDLE_FLAG_INHERIT):
        error = OSError(f"SetHandleInformation (desktop) error {win_api.last_error()}")
        win_desktop.close(handle)
        raise error
    return handle


def _release_into_job(k, job, process, thread) -> None:
    """Put the suspended process in the job and let it run. A process that cannot join the
    job, or cannot be resumed, is ended here: left alone it would sit suspended, outside any
    group, where no later stop could find it. Closes the thread handle; raises OSError."""
    try:
        if not k.AssignProcessToJobObject(job, process):
            raise OSError(f"AssignProcessToJobObject error {win_api.last_error()}")
        if k.ResumeThread(thread) == win_api.RESUME_FAILED:
            raise OSError(f"ResumeThread error {win_api.last_error()}")
    except OSError:
        # Not checked: the error on its way out is the one to report.
        _ = k.TerminateProcess(process, 1)
        raise
    finally:
        k.CloseHandle(thread)


def start(unit: str, argv: list[str], env: dict, log_path: Path,
          props: list[str]) -> Started:
    """Start `argv` inside a new job named `unit`, detached; return its pid (Started).

    The job gets the limits in `props` (caps()); a "Desktop=<name>" property makes that
    desktop and starts the process on it, and without one it starts on the user's own
    desktop. This process closes its own handles before returning: the session lives on in
    its job, on its desktop."""
    if shutil.which(argv[0], path=env.get("PATH")) is None:
        raise HarnessError(EXIT_LAUNCH, f"not an executable: {argv[0]}", "check the binary path")
    k = win_api.kernel32()
    job = process = desktop = None
    try:
        limits = _limits(props)
        job = _make_job(k, unit, limits)
        where = None
        if limits["desktop"]:
            desktop = _inheritable_desktop(k, limits["desktop"])
            where = win_desktop.path(limits["desktop"])
        with open(log_path, "ab") as log:
            inherit = [_inheritable_handle(log), job] + ([desktop] if desktop else [])
            process, thread, pid = _spawn(k, argv, env, where, inherit)
        _release_into_job(k, job, process, thread)
    except OSError as e:
        raise HarnessError(EXIT_LAUNCH, f"cannot start the session's process group: {e}",
                           f"see {log_path}") from e
    finally:
        for handle in (process, job):
            if handle:
                k.CloseHandle(handle)
        if desktop:
            win_desktop.close(desktop)
    return Started(pid)


def _wait_for_exit(k, process, argv: list[str], timeout: float) -> int:
    """Wait until the process ends and return its exit code; raise subprocess.TimeoutExpired
    when `timeout` seconds pass first. WHY many short waits and not one long one: Python acts
    on Ctrl-C only between calls, and a build may run for an hour."""
    deadline = time.monotonic() + timeout
    while k.WaitForSingleObject(process, _RUN_POLL_MS) == win_api.WAIT_TIMEOUT:
        if time.monotonic() >= deadline:          # bounded by the deadline
            raise subprocess.TimeoutExpired(argv, timeout)
    code = win_api.DWORD()
    if not k.GetExitCodeProcess(process, ctypes.byref(code)):
        raise OSError(f"GetExitCodeProcess error {win_api.last_error()}")
    return code.value


def run(unit: str, argv: list[str], log, props: list[str], timeout: float) -> int:
    """Run `argv` to its end inside a new job named `unit`, its output going to the open
    file `log`; return its exit code. The wait-for-it form, for `fsoctl build`. Windows only:
    on Linux `systemd-run --scope` is itself that form (build.py).

    What differs from start(), each on purpose:
    - the job ends everything in it when its last handle closes, and this process holds the
      only handle (the program inherits the log and nothing else). MSBuild leaves worker
      and symbol-server processes behind for minutes after a build; in a job that outlived
      it they would make the next build of the tree "already running". And a build whose
      `fsoctl` was killed must not keep compiling: closing is what a killed process does.
    - no desktop of its own: a build draws nothing.
    The job's name can be opened while this function runs, so `is_active(unit)` in another
    `fsoctl build` of the same tree sees it.

    Raises subprocess.TimeoutExpired after `timeout` seconds (the job is ended first, the
    exception subprocess.run raises on Linux), and HarnessError EXIT_LAUNCH when the program
    cannot be started or its exit code cannot be read.
    """
    if shutil.which(argv[0]) is None:
        raise HarnessError(EXIT_LAUNCH, f"not an executable: {argv[0]}",
                           "is it installed and on PATH?")
    k = win_api.kernel32()
    job = process = None
    try:
        job = _make_job(k, unit, {**_limits(props), "kill_on_close": True})
        process, thread, _pid = _spawn(k, argv, dict(os.environ), None,
                                       [_inheritable_handle(log)])
        _release_into_job(k, job, process, thread)
        try:
            return _wait_for_exit(k, process, argv, timeout)
        except subprocess.TimeoutExpired:
            # The result is not checked: closing the job below ends it in any case.
            _ = k.TerminateJobObject(job, 1)
            raise
    except OSError as e:
        raise HarnessError(EXIT_LAUNCH, f"cannot run {argv[0]} in its process group: {e}",
                           "this is unexpected; run the command again") from e
    finally:
        for handle in (process, job):
            if handle:
                k.CloseHandle(handle)


def _processes_in(k, job) -> int | None:
    """How many processes are in the open job, or None when Windows will not say."""
    info = win_api.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    if not k.QueryInformationJobObject(job, win_api.JOB_BASIC_ACCOUNTING_INFORMATION,
                                       ctypes.byref(info), ctypes.sizeof(info), None):
        return None
    return info.ActiveProcesses


def _may_be_there() -> bool:
    """After a failed OpenJobObjectW: False only when Windows says there is no job of that
    name; True for every other failure (for one, "access denied": a session started from an
    administrator prompt, asked about from an ordinary one). WHY (QA Plan B, CR-9 / ST-4 /
    TA-12): read as "gone", a job that could not be opened let cleanup remove a live
    session's folder and let `stop` report success while the engine ran on. "Gone" is a
    finding, never a lookup that failed. Which errors Windows gives for a job of another
    privilege level was not measured."""
    return win_api.last_error() != win_api.ERROR_FILE_NOT_FOUND


def is_active(unit: str) -> bool:
    """Return True while a job of that name exists and is not known to be empty: it has a
    process in it, or Windows will not say (the count cannot be read, or the job may not be
    opened from this login). Only a job that is not there, or is empty, is inactive."""
    k = win_api.kernel32()
    job = k.OpenJobObjectW(win_api.JOB_OBJECT_QUERY, False, unit)
    if not job:
        return _may_be_there()
    try:
        return _processes_in(k, job) != 0
    finally:
        k.CloseHandle(job)


def active_units() -> list[str]:
    """Return the harness session jobs that are running, as scope.py names them
    ("fso-harness-s1a2b3c4d.scope"). Windows cannot list jobs by name, so this asks about
    the job of every run folder under the runtime root; sessions of another runtime root are
    not seen (scope.py sees those too)."""
    from .. import paths  # noqa: PLC0415 - paths imports this package; see win_procs.ask_stop
    found = []
    for folder in sorted(paths.runtime_root().iterdir()):
        if paths.is_session_id(folder.name) and is_active(f"fso-harness-{folder.name}"):
            found.append(f"fso-harness-{folder.name}.scope")
    return found


def state(unit: str) -> str:
    """Return "active" or "inactive" (a job has no in-between states to report)."""
    return "active" if is_active(unit) else "inactive"


def is_gone(unit: str) -> bool:
    """Return True once nothing of the job is left running (or it cannot be found by name;
    see the module docstring for the one case where those differ)."""
    return not is_active(unit)


def stop(unit: str, timeout: float = POLITE_STOP_SEC) -> None:
    """End every process in the job; succeeds if it is already gone.

    This is the backstop, as on Linux: `fsoctl stop` asks the runner first, which closes the
    engine in an orderly way, and calls this afterwards for whatever is left. There is no
    polite form at this level, so the job is ended at once and `timeout` (at least
    _KILL_WAIT) bounds the wait for it to empty. Raises EXIT_ENGINE if it does not, if
    Windows will not say whether it did, or if the job exists and this login may not manage
    it. A job whose process count cannot be read is ended all the same: not knowing is no
    reason to leave it."""
    k = win_api.kernel32()
    job = k.OpenJobObjectW(win_api.JOB_OBJECT_QUERY | win_api.JOB_OBJECT_TERMINATE, False, unit)
    if not job:
        if _may_be_there():
            raise HarnessError(EXIT_ENGINE, f"job {unit} may exist, but this login may not "
                               f"manage it (Windows error {win_api.last_error()})",
                               "if it was started from an administrator prompt or another "
                               "account, stop the session from there")
        return
    try:
        left = _processes_in(k, job)
        if left == 0:
            return
        # The result is not checked: it fails when the job emptied in the meantime, which is
        # the outcome wanted; the count below decides either way.
        _ = k.TerminateJobObject(job, 1)
        deadline = time.monotonic() + max(timeout, _KILL_WAIT)
        while time.monotonic() < deadline:        # bounded by the deadline
            left = _processes_in(k, job)
            if left == 0:
                return
            time.sleep(0.1)
        count = "an unknown number of" if left is None else left
        raise HarnessError(EXIT_ENGINE, f"job {unit} still has {count} process(es) after it "
                           "was ended", "check Task Manager for a process that will not end; "
                           "a reboot clears it")
    finally:
        k.CloseHandle(job)
