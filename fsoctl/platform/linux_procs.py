"""Facts about processes on Linux, read from /proc and `ss` (moved here from launcher.py,
match.py and renderer.py, bodies unchanged: cross-platform Plan B, Task 2).

win_procs.py is the Windows twin (same functions, same signatures).
"""
import os
import re
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

_SS_PID = re.compile(r"pid=(\d+),")


def pid_alive(pid: int) -> bool:
    """True while a process with this pid exists (signal 0 probes without signalling)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pid_ended(pid: int) -> bool:
    """True only when it is certain that no process has this pid. A process that exists and
    is not ours to signal answers False (pid_alive calls that one "not alive")."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def pid_start(pid: int) -> int | None:
    """Return the process's start time (clock ticks since boot) from /proc, or None if it is gone.

    /proc/<pid>/stat: "pid (comm) state ..." and comm may contain spaces or parentheses, so the
    fields are taken after the LAST ')'; field 22 of the man page is then index 19.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    rest = text.rsplit(")", 1)[1].split()
    return int(rest[19])


def _cgroup_has_scope(cgroup_text: str, unit: str) -> bool:
    """True when /proc/<pid>/cgroup text places the process in `<unit>.scope` (exact name)."""
    return any(line.rsplit("/", 1)[-1] == f"{unit}.scope" for line in cgroup_text.splitlines())


def in_group(pid: int, unit: str) -> bool:
    """True when process `pid` exists and lives in the scope `unit`.

    WHY check: runner_pid is a stored number; after the session ended it may belong to an
    unrelated process, which must never be signalled.
    """
    try:
        return _cgroup_has_scope(Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8"), unit)
    except OSError:
        return False


def gone(pid: int) -> bool:
    """True when `pid` no longer runs (absent, or a zombie waiting for its parent to reap it)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return True
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"   # field 3; the name in () may hold spaces


def ask_stop(sess) -> str | None:
    """Ask the session's runner to end the engine and exit: SIGTERM to sess.runner_pid. The
    caller has checked that the pid is the runner (launcher._runner_is_ours). Returns None
    when asked, or "skipped:<why>" when the signal could not be sent (the runner went away in
    the meantime, or is not ours to signal); the scope stop that follows handles those."""
    try:
        os.kill(sess.runner_pid, signal.SIGTERM)
    except OSError as e:
        return f"skipped:{e.strerror or e}"
    return None


def _host(argv: list[str]) -> subprocess.CompletedProcess:
    """Run a short query on the host, bounded, text output."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)


def socket_owner_pids(proto: str, port: int,
                      run: Callable[[list[str]], subprocess.CompletedProcess] | None = None
                      ) -> set[int]:
    """The pids that hold a listening `proto` ("udp" | "tcp") socket on `port`, from
    `ss -H -l{u,t}np "sport = :<port>"` (-p names the owning pid). `run` runs the query
    somewhere other than the host: a private match's sockets exist only inside its network
    namespace, so the launcher passes a function that runs argv there. An `ss` that fails
    owns nothing."""
    flag = "-lunp" if proto == "udp" else "-ltnp"
    r = (run or _host)(["ss", "-H", flag, f"sport = :{port}"])
    if r.returncode != 0:
        return set()
    return {int(pid) for pid in _SS_PID.findall(r.stdout)}


def loaded_libraries(pid: int) -> str:
    """Return /proc/<pid>/maps text (it names every mapped library), or "" if the process is
    gone or unreadable. renderer.classify reads it."""
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
