"""Probe 10 (cross-platform Plan B, before Task 4): can a NAMED Windows Job Object still be
opened by name after the process that created it has exited, while a process is still in it?

Why it matters: `fsoctl launch` creates the session's job and exits; a later `fsoctl stop` or
`status` is a new process that has only the name. Plan B Task 4 was written assuming the name
stays as long as a process is in the job. Windows removes an object's NAME when its last
HANDLE closes, even while the object itself lives on, so that had to be measured.

Three cases, each with a child that sleeps and a creator that exits:
  plain     the creator holds the only handle
  inherit   the child inherits the creator's job handle (it holds one for as long as it lives)
  reopen    the child opens the job by name itself before the creator exits

    python win_job_name.py --out <folder>          (Windows only; prints and writes JSON)
"""
import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

JOB_OBJECT_QUERY, JOB_OBJECT_TERMINATE = 0x0004, 0x0008
JOB_ALL = 0x1F001F
CHILD_SLEEP_S = 25
CREATOR = "creator"


def kernel32():
    """kernel32 with the job calls typed (handles are pointer-sized)."""
    from ctypes import wintypes as wt  # noqa: PLC0415 - Windows-only
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = wt.HANDLE, [ctypes.c_void_p,
                                                                         wt.LPCWSTR]
    k.OpenJobObjectW.restype, k.OpenJobObjectW.argtypes = wt.HANDLE, [wt.DWORD, wt.BOOL,
                                                                     wt.LPCWSTR]
    k.AssignProcessToJobObject.restype = wt.BOOL
    k.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k.SetHandleInformation.restype = wt.BOOL
    k.SetHandleInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD]
    k.TerminateJobObject.restype, k.TerminateJobObject.argtypes = wt.BOOL, [wt.HANDLE, wt.UINT]
    k.CloseHandle.restype, k.CloseHandle.argtypes = wt.BOOL, [wt.HANDLE]
    k.OpenProcess.restype, k.OpenProcess.argtypes = wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]
    return k


def child(name: str, mode: str) -> int:
    """The process that stays in the job: optionally opens the job by name, says so, sleeps."""
    held = None
    if mode == "reopen":
        held = kernel32().OpenJobObjectW(JOB_ALL, False, name)
    print(f"child ready held={bool(held)}", flush=True)
    time.sleep(CHILD_SLEEP_S)
    return 0


def creator(name: str, mode: str, pid_file: str) -> int:
    """Create the named job, put a child in it, wait until the child is ready, exit."""
    k = kernel32()
    job = k.CreateJobObjectW(None, name)
    if not job:
        print(f"CreateJobObjectW failed {ctypes.get_last_error()}")
        return 2
    if mode == "inherit":
        k.SetHandleInformation(job, 1, 1)             # HANDLE_FLAG_INHERIT
    proc = subprocess.Popen([sys.executable, __file__, "--child", name, mode],
                            stdout=subprocess.PIPE, text=True, close_fds=(mode != "inherit"))
    handle = k.OpenProcess(0x1F0FFF, False, proc.pid)
    assigned = bool(k.AssignProcessToJobObject(job, handle))
    ready = proc.stdout.readline().strip()
    Path(pid_file).write_text(json.dumps({"child": proc.pid, "assigned": assigned,
                                          "ready": ready}))
    return 0                                          # the creator's handle closes here


def opens_by_name(k, name: str) -> dict:
    """Try to open the job by name and, if that works, end it; report both."""
    job = k.OpenJobObjectW(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, False, name)
    if not job:
        return {"opened": False, "error": ctypes.get_last_error()}
    ended = bool(k.TerminateJobObject(job, 1))
    k.CloseHandle(job)
    return {"opened": True, "terminated": ended}


def alive(pid: int) -> bool:
    """True while pid shows in tasklist."""
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                         text=True, timeout=30, check=False).stdout
    return str(pid) in out


def main() -> int:
    """Run the three cases and write probe-win-job-name.json."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path)
    ap.add_argument("--child", nargs=2)
    ap.add_argument("--creator", nargs=3)
    args = ap.parse_args()
    if args.child:
        return child(*args.child)
    if args.creator:
        return creator(*args.creator)
    k = kernel32()
    results = {}
    for mode in ("plain", "inherit", "reopen"):       # bounded: three cases
        name = f"fsoharness-probe-{mode}-{os.getpid()}"
        pid_file = str(args.out / f"job-{mode}.json")
        code = subprocess.run([sys.executable, __file__, "--creator", name, mode, pid_file],
                              timeout=60, check=False).returncode
        made = json.loads(Path(pid_file).read_text())
        time.sleep(1.0)                               # the creator has exited by now
        row = {"creator_exit": code, **made, "child_alive_before": alive(made["child"])}
        row.update(opens_by_name(k, name))
        time.sleep(1.0)
        row["child_alive_after"] = alive(made["child"])
        if row["child_alive_after"]:
            subprocess.run(["taskkill", "/F", "/PID", str(made["child"])], capture_output=True,
                           timeout=30, check=False)
        results[mode] = row
    out = {"probe": "win-job-name", "python": sys.version.split()[0], "cases": results}
    (args.out / "probe-win-job-name.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
