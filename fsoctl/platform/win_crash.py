"""The engine's own crash dump on Windows, and a backtrace from it.

Linux gets a crashed engine's core from systemd-coredump and reads it with gdb (crash.py).
Windows has neither. What it has is the engine's own handler: for an exception nothing
handled, FSO writes a minidump named `<exe name without .exe>_YYYYMMDD_HHMMSS.mdmp` beside
its executable, stamped in UTC, and lets the exception end the process
(code/globalincs/crashdump.cpp, read in the 26.2.0 tree). This module finds that file for a
session and, when Microsoft's console debugger cdb is installed, asks it for the stack.

Measured in the VM on 2026-11-16 (session s8bc8c2e3, FSO 26.2.0 FastDebug; results note,
"Crash reports"): a fatal Lua API call ended the engine with exit code 0xE06D7363 and left
`fs2_open_26_2_0_x64_SSE2-FASTDBG_20261117_072630.mdmp`; the dump's MiscInfo stream held the
engine's process id; cdb 10.0.26100 printed the stack only after `.ecxr` (without it the
stack is the dump writer's own three frames).
"""
import calendar
import itertools
import ntpath
import os
import re
import stat
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from . import programs
from . import win_scope as scope

# A dump's name carries the second it was written in; the runner records the end a moment
# later. A dump stamped more than this many seconds after the recorded end is not ours.
_AFTER_END_S = 5
_SCAN_MAX = 20000                  # files looked at beside the engine before giving up
_HEAD_MAX = 64 * 1024              # bytes of a dump read to find its process id
_STREAMS_MAX = 64                  # a real dump has about 14 streams
_MISC_INFO_STREAM = 15             # MINIDUMP_STREAM_TYPE MiscInfoStream
_MISC1_PROCESS_ID = 0x1            # MiscInfo Flags1 bit: the ProcessId field is valid
# The same limits as crash.py's gdb run; tests hold the two sets equal. Not imported from
# there: crash.py imports the platform package, which imports this module on Windows.
DEBUGGER_TIMEOUT = 180
BT_FRAMES = 30
BT_MAX_LINES = 400
_FRAME_ROW = re.compile(r"[0-9a-f]{8}`[0-9a-f]{8} ")     # see _frames
NO_DEBUGGER = "install the Debugging Tools for Windows (cdb) to get a backtrace"
SYMBOLS_NOTE = ("names in the backtrace are exact only with the engine's .pdb file beside "
                "it; without one cdb shows the nearest exported name plus an offset")
# Exit codes of a process an exception ended. The first four names are Windows' own
# (`certutil -error <code>`, checked in the VM); the last is the Microsoft C++ runtime's
# exception code, which cdb reports as "C++ EH exception".
EXCEPTION_CODES = {
    0xC0000005: "access violation (STATUS_ACCESS_VIOLATION)",
    0xC00000FD: "stack overflow (STATUS_STACK_OVERFLOW)",
    0xC0000409: "stack buffer overrun or fast fail (STATUS_STACK_BUFFER_OVERRUN)",
    0xC0000374: "heap corruption (STATUS_HEAP_CORRUPTION)",
    0xE06D7363: "a C++ exception nothing caught (a fatal Lua API error under "
                "-noninteractive ends the engine this way)",
}


def dump_pid(head: bytes) -> int | None:
    """Return the process id recorded in a minidump whose first bytes are `head`, or None if
    they are not a minidump, hold no MiscInfo stream, or point outside `head`.

    Layout (minidumpapiset.h; checked against the real dump above): a 32-byte header whose
    third and fourth 32-bit fields are the stream count and the offset of the stream
    directory; directory entries of 12 bytes (type, size, offset); the MiscInfo stream
    starts with its size, a flags word and the process id.
    """
    if len(head) < 32 or head[:4] != b"MDMP":
        return None
    count, directory = struct.unpack_from("<II", head, 8)
    for i in range(min(count, _STREAMS_MAX)):      # bounded: a damaged count cannot spin
        entry = directory + 12 * i
        if entry + 12 > len(head):
            return None
        kind, _size, where = struct.unpack_from("<III", head, entry)
        if kind != _MISC_INFO_STREAM:
            continue
        if where + 12 > len(head):
            return None
        _info_size, flags, pid = struct.unpack_from("<III", head, where)
        return pid if flags & _MISC1_PROCESS_ID else None
    return None


def _read_head(path: str) -> bytes:
    """Return the first _HEAD_MAX bytes of a dump file ("" if it cannot be read)."""
    try:
        with open(path, "rb") as f:
            return f.read(_HEAD_MAX)
    except OSError:
        return b""


def _stamp_time(day: str, clock: str) -> int | None:
    """Return the epoch second of a dump name's stamp, read as UTC (the engine stamps with
    GetSystemTime), or None if the digits are not a date."""
    try:
        return calendar.timegm(time.strptime(day + clock, "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def find_dump(sess) -> dict | None:
    """Return the session engine's crash dump as {"path", "size", "time", "pid"}, or None.

    A file counts when its name is the engine's executable name with `_YYYYMMDD_HHMMSS.mdmp`
    in place of `.exe` (any letter case: Windows file names have none), it is a plain file
    and not a link, its stamp is not before the second the session started in, and the
    process id recorded inside it is the session engine's. The newest such file wins.
    "time" is the stamp as an epoch second; "pid" is None when the file's process id could
    not be read, in which case name and time alone decided.

    WHY the process id: the name carries none, and two sessions of one build at once is
    the harness's normal use. By name and time alone the later crash would be reported for
    both sessions, and the debugger would then explain the wrong one. Linux matches a core
    by pid for the same reason (crash.py, _is_ours).

    Raises OSError when the engine's folder holds more than _SCAN_MAX files (the caller
    turns that into a hint): a scan with no bound is not started.
    """
    exe = Path(sess.binary)
    name = re.compile(re.escape(exe.stem) + r"_(\d{8})_(\d{6})\.mdmp", re.IGNORECASE)
    # When the run was recorded as over, if it was: a dump stamped later is not this
    # session's, even with the same process id in it (ids are reused; cross-family XFA-5).
    ended = (getattr(sess, "exit_info", None) or {}).get("at")
    try:
        listing = os.scandir(exe.parent)
    except OSError:
        return None
    best = None
    with listing:
        for n, entry in enumerate(itertools.islice(listing, _SCAN_MAX + 1)):
            if n >= _SCAN_MAX:
                raise OSError(f"more than {_SCAN_MAX} files beside the engine in "
                              f"{exe.parent}; its crash dump was not looked for")
            m = name.fullmatch(entry.name)
            stamp = _stamp_time(*m.groups()) if m else None
            if stamp is None or stamp < int(sess.created):
                continue
            if ended is not None and stamp > int(ended) + _AFTER_END_S:
                continue             # written after this session was over: another process
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):         # a link or a folder is never a dump
                continue
            pid = dump_pid(_read_head(entry.path))
            if pid is not None and pid != sess.engine_pid:
                continue
            if best is None or stamp > best["time"]:
                best = {"path": entry.path, "size": info.st_size, "time": stamp, "pid": pid}
    return best


# Environment names the debugger reads, by their starts, in capitals. Microsoft's list of the
# debugger's variables has, beside the two symbol paths, an image path "used in some
# situations such as user mode minidump files" (this case), a source path, an extension path
# and log files; any of them can name a server (QA Plan B, SA-7). cdb gets none of them.
_DEBUGGER_NAMES = ("_NT_", "DBGHELP_", "DBGENG_", "SRCSRV_")


def find_debugger() -> str | None:
    """Return the path of cdb.exe, or None: the one on PATH, else the one in the folder the
    Windows SDK's installer uses. WHY the second place: that installer does not add cdb to
    PATH (measured in the VM: installed, `where cdb` found nothing), so looking on PATH only
    would tell a user who has the tools to install them."""
    found = programs.find("cdb")              # PATH only, never the current folder
    if found:
        return found
    # Windows reads environment names in any letter case; Python's os.environ there keeps
    # them in capitals.
    for root in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES")):
        if root:
            candidate = Path(root) / "Windows Kits" / "10" / "Debuggers" / "x64" / "cdb.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _frames(output: str) -> list[str]:
    """Pick the stack out of cdb's output: the frame rows between the "Child-SP" header and
    "quit:". A frame row starts with two 64-bit addresses written as 8 hex digits, a
    backquote and 8 more; cdb also prints warnings between rows as it loads each module
    (the real output has one for SDL3.dll), and those are left out. If no frame row is found
    (another cdb version or target, or the command failed), return what cdb said after
    reading our command instead, so the reader sees its message."""
    lines = [ln.rstrip() for ln in output.splitlines()]
    if "quit:" in lines:
        lines = lines[:lines.index("quit:")]
    header = next((i for i, ln in enumerate(lines) if ln.startswith("Child-SP")), None)
    if header is not None:
        rows = [ln for ln in lines[header + 1:] if _FRAME_ROW.match(ln)]
        if rows:
            return rows
    echo = next((i for i, ln in enumerate(lines) if "Reading initial command" in ln), -1)
    return [ln for ln in lines[echo + 1:] if ln.strip()]


def backtrace(dump: dict) -> list[str] | None:
    """Return the crashing thread's stack from the dump as cdb prints it (at most
    BT_FRAMES frames, BT_MAX_LINES lines), or None when cdb is not installed, fails, takes
    longer than DEBUGGER_TIMEOUT seconds, or prints nothing.

    The command is `.ecxr; k 0nN; q`. `.ecxr` switches to the exception's own context:
    without it the stack is that of the code writing the dump (three frames inside the dump
    writer for the real dump above). `0n` marks the frame count as decimal: cdb reads a
    bare number as hex, and `k 30` printed all 38 frames of that dump.

    Three precautions, matching the gdb run on Linux (crash.py, _backtrace):
    - no symbol download: every variable the debugger family reads (_DEBUGGER_NAMES) is
      taken out of cdb's environment and the symbol path is the engine's own folder (`-y`),
      where a build's .pdb sits beside its executable;
    - no startup script: started without `-c`, cdb ran an `ntsd.ini` found in its working
      folder; started with `-c`, as here, it did not (both measured on cdb 10.0.26100, with
      controls). Nothing documents that `-c` switches the file off, so the working folder
      is also a new, empty one, removed afterwards: the caller's folder may be a mod under
      test;
    - bounded: a time limit, and the output is cut to BT_MAX_LINES lines.
    """
    cdb = find_debugger()
    if cdb is None:
        return None
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_DEBUGGER_NAMES)}
    with tempfile.TemporaryDirectory(prefix="fso-harness-cdb-") as empty:
        try:
            # ntpath, by name: it is os.path on Windows, and it also splits a Windows path
            # when this module's tests run on Linux.
            r = subprocess.run([cdb, "-z", dump["path"], "-y", ntpath.dirname(dump["path"]),
                                "-c", f".ecxr; k 0n{BT_FRAMES}; q"],
                               capture_output=True, text=True, errors="replace",
                               timeout=DEBUGGER_TIMEOUT,
                               check=False, env=env, cwd=empty)
        except (OSError, subprocess.TimeoutExpired):
            return None
    return _frames(r.stdout)[:BT_MAX_LINES] or None


def end_hints(sess, dump: dict | None, gone: bool | None = None) -> list[str]:
    """Explain how a Windows run ended: what the exit code means, why there may be no dump,
    and what the harness itself ended. The Windows twin of crash.py's _end_hints, which
    speaks of signals, systemd scopes and the journal.

    `gone` is the caller's answer to "is nothing of this session running any more"
    (launcher._gone, which also asks about the recorded runner and engine: from another
    Windows login a live session's job is "not found" too). None asks the job alone."""
    hints = []
    # The job is asked only for a record with no end, and only when the caller gave no answer.
    if sess.status in ("starting", "running") and (scope.is_gone(sess.scope) if gone is None
                                                    else gone):
        hints.append("the session's processes are gone but its end was never recorded (the "
                     "runner was ended from outside); see runner.log in the session's run "
                     "folder")
    if sess.ended_by == "runtime_limit":
        hints.append(f"the runtime limit ({sess.runtime_sec} s) ended the run; launch with a "
                     "larger --runtime")
    if sess.ended_by == "abandoned_start":
        hints.append("the session never finished starting (its launch was ended before the "
                     "engine ran); a later cleanup ended what was left of it")
    if sess.ended_by == "runner_lost":
        hints.append("its runner was gone while the engine or a child of it still ran (the "
                     "runner was ended from outside, or crashed); a later cleanup ended what "
                     "was left; see runner.log in the session's run folder")
    if sess.ended_by == "runner_error":
        hints.append("the harness's own runner failed after starting the engine and ended "
                     "it; the runner's error is in runner.log in the session's run folder")
    code = (sess.exit_info or {}).get("returncode")
    # An exception's code is the exit code of the process it ends, and those codes have the
    # top bit set; the engine's own exit codes are small numbers.
    if isinstance(code, int) and code >= 0x80000000:
        meaning = EXCEPTION_CODES.get(code, "a Windows exception or error status")
        hints.append(f"exit code {code} is 0x{code:08X}: {meaning}")
        if dump is None:
            hints.append("no crash dump found: the engine writes one beside its executable, "
                         "only for an exception that reaches its own handler and only if "
                         "it may write to that folder")
    return hints
