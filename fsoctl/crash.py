"""crash_report: everything known about how a session's engine ended.

Exit status and signal (recorded by the runner), the core dump systemd-coredump kept for the
engine's pid, a gdb backtrace read from that core IN PLACE (cores of a debug build were ~700 MB in
the probes, so the core is never copied), the log tail and its problems, the popups the bridge saw,
and the last bridge heartbeat. A debug engine build gives far better backtraces and logs.
"""
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import channel, launcher, logs, scope, session
from .cli import register
from .errors import HarnessError

_TOOL_TIMEOUT = 60
_GDB_TIMEOUT = 180      # loading a large debug core takes a while
_BT_FRAMES = 30
_BT_MAX_LINES = 400   # all threads x 30 frames can run to hundreds of lines; keep reports sane
_LOG_TAIL = 60
# Signals whose default action dumps core (signal(7), "Core" column).
_CORE_SIGNALS = {"SIGABRT", "SIGBUS", "SIGFPE", "SIGILL", "SIGQUIT", "SIGSEGV", "SIGSYS",
                 "SIGTRAP", "SIGXCPU", "SIGXFSZ"}
_CORE_WAIT = 10.0   # seconds to keep looking for a core that systemd-coredump may still be writing
_CORE_POLL = 1.0
_CORE_LOG_GRACE = 300.0   # seconds a dump may be logged after the engine's recorded exit


def _coredumps_json(since: float) -> str:
    """Return coredumpctl's JSON list of dumps since the given epoch time ("[]" if none)."""
    r = subprocess.run(["coredumpctl", "--no-pager", "--json=short", "list",
                        f"--since=@{int(since)}"], capture_output=True, text=True,
                       timeout=_TOOL_TIMEOUT, check=False)
    return r.stdout if r.returncode == 0 and r.stdout.strip() else "[]"


def _is_ours(row: dict, pid: int, exe: str | None, exited_at: float | None) -> bool:
    """True if a coredumpctl row is the session engine's dump, not a reused pid's (XK-7).

    Checks the pid, the executable path (when `exe` is given) and that the dump was logged no
    later than _CORE_LOG_GRACE seconds after the engine's recorded exit (when known). WHY a grace
    period and not "logged before the exit": coredumpctl's "time" is the journal time of the
    dump, written after the core is stored; for the 700 MB debug core in tests/data it was 7 s
    after the crash (journal COREDUMP_TIMESTAMP 03:50:59 vs entry 03:51:06, checked 2026-10-12).
    """
    if row.get("pid") != pid:
        return False
    if exe is not None and row.get("exe") != exe:
        return False
    logged = (row.get("time") or 0) / 1e6
    return exited_at is None or logged <= exited_at + _CORE_LOG_GRACE


def parse_coredumpctl(text: str, pid: int, exe: str | None = None,
                      exited_at: float | None = None) -> dict | None:
    """Return the dump entry for `pid` from coredumpctl JSON, or None.

    Receives: exe (the engine's resolved path; only a dump of that executable matches) and
    exited_at (exit_info["at"]; dumps logged long after it are someone else's), both optional.
    """
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and _is_ours(row, pid, exe, exited_at):
            sig = row.get("sig")
            try:
                name = signal.Signals(sig).name
            except (TypeError, ValueError):
                name = str(sig)
            return {"pid": pid, "signal": name, "corefile": row.get("corefile"),
                    "exe": row.get("exe"), "time": (row.get("time") or 0) / 1e6,
                    "size": row.get("size")}
    return None


def _backtrace(core: dict) -> list[str] | None:
    """Return gdb's backtrace frames from the matched core (read in place), or None.

    Receives the entry _find_core matched (pid, exe, logged time). WHY not `debug <pid>`:
    coredumpctl then opens the newest dump with that pid, which after pid reuse is another
    program's (QA CR2-12 / Y-11). Matches on different journal fields are ANDed, and the
    --since/--until second around the logged time picks this dump (both checked against a real
    dump 2026-10-12: a wrong exe or a window outside it finds nothing).
    """
    env = {**os.environ, "DEBUGINFOD_URLS": ""}   # never fetch symbols over the network
    gdb_args = f"-batch -ex 'thread apply all bt {_BT_FRAMES}'"
    logged = int(core["time"])
    try:
        r = subprocess.run(["coredumpctl", "--no-pager", f"--since=@{logged}",
                            f"--until=@{logged + 1}", "debug", f"COREDUMP_PID={core['pid']}",
                            f"COREDUMP_EXE={core['exe']}", f"--debugger-arguments={gdb_args}"],
                           capture_output=True, text=True, timeout=_GDB_TIMEOUT, check=False,
                           env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    frames = [ln.strip() for ln in r.stdout.splitlines() if ln.startswith("#")][:_BT_MAX_LINES]
    return frames or None


def _artifacts(sess: session.Session) -> dict:
    """Return the log tail and problems, popups seen, and the last bridge heartbeat."""
    out: dict = {"log_tail": [], "problems": [], "popups": [], "last_heartbeat": None,
                 "log_error": None}
    try:
        log = logs.read(sess.id, tail=_LOG_TAIL)
        out["log_tail"], out["problems"] = log["lines"], log["problems"]
    except HarnessError as e:   # no log yet: say so in the report instead of failing it
        out["log_error"] = e.message
    if sess.kind == "game":
        ch = channel.Channel(sess)
        popups = ch.dir / "popups.txt"
        if popups.exists():
            out["popups"] = [json.loads(x) for x in popups.read_text().splitlines() if x.strip()]
        out["last_heartbeat"] = ch.heartbeat()
    return out


def _find_core(sess: session.Session, sig: str | None) -> dict | None:
    """Return coredumpctl's entry for the engine's pid, or None.

    When the engine died of a signal that dumps core, keep asking for up to _CORE_WAIT seconds:
    systemd-coredump records the dump only after writing it, and a report run right after the
    crash used to find nothing (QA TA-7).
    """
    if not sess.engine_pid:
        return None
    deadline = time.monotonic() + (_CORE_WAIT if sig in _CORE_SIGNALS else 0)
    while True:   # bounded by the deadline
        core = parse_coredumpctl(_coredumps_json(sess.created), sess.engine_pid,
                                 exe=os.path.realpath(sess.binary),
                                 exited_at=(sess.exit_info or {}).get("at"))
        if core or time.monotonic() >= deadline:
            return core
        time.sleep(_CORE_POLL)


def _end_hints(sess: session.Session, sig: str | None, core: dict | None) -> list[str]:
    """Explain how the run ended when the scope, not the engine, ended it (QA SL-7 / SF-4)."""
    journal = f"`journalctl --user -u {sess.scope}.scope`"
    hints = []
    if sess.status in ("starting", "running") and scope.is_gone(sess.scope):
        hints.append("the session was killed with its scope (runtime limit / OOM / external "
                     f"stop) before its end could be recorded; see {journal}")
    if sess.ended_by == "runtime_limit":
        hints.append(f"the scope's runtime limit ({sess.runtime_sec} s) ended the run; launch "
                     "with a larger --runtime")
    elif sess.ended_by == "oom":
        hints.append(f"the scope hit its memory limit (MemoryMax={sess.memory_max}) and the "
                     f"kernel's OOM killer ended a process; see {journal}")
    elif sig == "SIGKILL" and sess.ended_by != "stop":
        hints.append(f"SIGKILL usually means the scope's memory limit (MemoryMax="
                     f"{sess.memory_max}) or a stop that had to force-kill; see {journal}")
    if sig in _CORE_SIGNALS and not core:
        hints.append("no core dump found for the engine yet; systemd-coredump may still be in "
                     "progress (run crash_report again in a minute), or size/storage limits "
                     "dropped it (`coredumpctl list`)")
    return hints


def report(sid: str) -> dict:
    """Collect exit info, core, backtrace, log problems, popups and heartbeat for a session."""
    sess = session.load(sid)
    if launcher.alive(sess):
        return {"id": sid, "status": "running",
                "hints": ["the engine is still running; stop it or wait for it to exit first"]}
    exit_info = sess.exit_info or {}
    sig = exit_info.get("signal")
    core = _find_core(sess, sig)
    hints = _end_hints(sess, sig, core)
    backtrace = None
    if core and core.get("corefile") == "present":
        backtrace = _backtrace(core)
        if not backtrace:
            hints.append("gdb produced no backtrace; is gdb installed?")
    if not Path(logs.sources(sess)["fso"]).exists():
        hints.append("release build: no fs2_open.log; a debug build (FSO_BIN_DEBUG) logs much more")
    return {"id": sid, "status": sess.status, "exit_info": sess.exit_info,
            "ended_by": sess.ended_by, "signal": sig, "core": core, "backtrace": backtrace,
            **_artifacts(sess), "hints": hints}


def _cfg_crash(p: argparse.ArgumentParser) -> None:
    """Add the `crash_report` verb's arguments."""
    p.add_argument("id")


@register("crash_report", "how a session's engine ended: signal, core, backtrace, log", _cfg_crash)
def _cli_crash(args: argparse.Namespace) -> dict:
    """CLI wrapper for report()."""
    return report(args.id)
