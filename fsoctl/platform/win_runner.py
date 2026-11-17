"""The process that runs INSIDE a Windows session's job and on its desktop: it starts the
engine, waits, and records how the run ended. The Windows counterpart of fsoctl/runner.py.

WHY a runner process (the same reasons as on Linux): something must outlive `fsoctl launch`,
own the engine, end it in an orderly way, and write down how it ended, which crash_report and
status read later. Started by launcher.launch as `python -m fsoctl.platform.win_runner
<session id>` through win_scope.start.

How it differs from the Linux runner, and why:
- No display server to start: the session's desktop already exists when this process starts
  on it (win_scope.start), and the engine, started with no desktop named, lands on the same one.
- It is told to stop by a FILE, not a signal. Windows has no SIGTERM for a process without a
  console; `fsoctl stop` leaves the stop marker (session.STOP_MARKER, `stop-requested`) in
  the run folder and this process looks for it twice a second.
- It enforces the runtime limit itself. A Linux scope has RuntimeMaxSec as a backstop; a job
  has no wall-clock limit, so if this process dies, the engine runs until the next launch
  or cleanup ends it (launcher._end_unattended) or someone stops the session.
- The engine is started WITH handle inheritance, so it carries the job and desktop handles
  this process inherited: the job stays findable by name while the engine lives (probe 10).
- No sandbox, no private home folder: the engine gets FSO_PREFERENCES_PATH and otherwise this
  process's environment (plan B call 3).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from .. import envsetup, launcher, session
from . import win_desktop, win_procs as procs

RUNNER_POLL_S = 0.5          # how often the engine, the stop file and the clock are looked at
ENGINE_CLOSE_WAIT_S = 20.0   # how long the engine gets to close after being asked
ENGINE_KILL_WAIT_S = 20.0    # how long to wait for it to be gone after being ended
ENDED_BY_HARNESS = 143       # main's result when the harness ended the run (as on Linux)


def _ask_close(pid: int) -> int:
    """Ask the engine to close: a close request to each of its windows. This process runs on
    the session's desktop, so its own thread sees them. Returns how many windows were asked."""
    return win_desktop.ask_close(pid)


def _end_engine(engine) -> int | None:
    """Close the engine in an orderly way, then by force if it does not go: ask its windows
    to close and wait ENGINE_CLOSE_WAIT_S; if it is still running, or it had no window to
    ask, end the process and wait ENGINE_KILL_WAIT_S. Returns its exit code, or None if it
    outlived both (recorded without a code; the job stop that follows `fsoctl stop` ends
    whatever is left)."""
    asked = 0
    try:
        asked = _ask_close(engine.pid)
    except OSError as e:
        _say(f"win_runner: could not ask the engine to close: {e}\n")
    if asked:
        try:
            return engine.wait(timeout=ENGINE_CLOSE_WAIT_S)
        except subprocess.TimeoutExpired:
            _say("win_runner: the engine did not close when asked; ending it\n")
    else:
        # Nobody was asked (an engine still starting has no window): waiting 20 s for an
        # answer to no question only delays the end.
        _say("win_runner: no window was asked to close; ending the engine\n")
    engine.kill()
    try:
        return engine.wait(timeout=ENGINE_KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        return None


def wait_for_end(engine, stop_file: Path, runtime_sec: int | None,
                 started: float) -> tuple[str | None, int | None]:
    """Wait for the first of: the engine exits, the stop file appears, the runtime limit
    passes. Return (cause, exit code): cause None when the engine exited by itself, "stop" or
    "runtime_limit" when this process ended it (code None if it could not be ended).

    Every session has a runtime limit (launch refuses one without), which bounds this loop;
    a record with none (not made by launch) waits for the engine or a stop."""
    while True:   # bounded by runtime_sec; see above
        code = engine.poll()
        if code is not None:
            return None, code
        if stop_file.exists():
            return "stop", _end_engine(engine)
        if runtime_sec and time.monotonic() - started >= runtime_sec:
            return "runtime_limit", _end_engine(engine)
        time.sleep(RUNNER_POLL_S)


def _record_exit(sid: str, code: int | None, cause: str | None) -> None:
    """Store how the run ended, in the Linux runner's fields: exit_info {returncode, signal,
    at} when the code is known, ended_by ("exit" when the engine ended by itself, else the
    cause), status "stopped" for a stop and "exited" otherwise (a "stopped" status is kept).
    signal is always None: Windows processes end with a code, not a signal."""
    ended_by = cause or "exit"

    def apply(sess: session.Session) -> None:
        if ended_by == "stop":
            sess.status = "stopped"
        elif sess.status != "stopped":
            sess.status = "exited"
        if code is not None:
            sess.exit_info = {"returncode": code, "signal": None, "at": time.time()}
        if sess.ended_by is None:
            sess.ended_by = ended_by
    session.update(sid, apply)


def _fail(sid: str, error: str) -> int:
    """Mark the session failed with `error` and return the runner's failure code (1)."""
    def apply(s: session.Session) -> None:
        s.status, s.exit_info = "failed", {"error": error, "at": time.time()}
    session.update(sid, apply)
    return 1


def _end_after_failure(sid: str, engine, error: BaseException) -> None:
    """End an engine this runner started and can no longer look after, and record the end
    as ended_by "runner_error".

    WHY (QA Plan B, CR-1 / ST-2): on Windows this process is the only thing that ends the
    engine at its runtime limit or on a stop request (a job has no time limit of its own).
    A runner that left with the engine running would leave it running for good. Runs only
    when the runner fails after the engine started; says so on stderr (runner.log). The
    record is written if it can be: a record that cannot be written may be the failure.

    The end is by force and at once, and only if the engine still runs: this is not an
    orderly stop (_end_engine, 20 s of waiting on a close request), and an engine that has
    exited must not have "its" windows asked anything, since its pid may be another
    program's by now. Nothing here may raise: the first error is the one to report.
    """
    _say(f"win_runner: ending the engine: the runner failed after starting it ({error!r})\n")
    code = None
    try:
        if engine.poll() is None:
            engine.kill()
        code = engine.wait(timeout=ENGINE_KILL_WAIT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        _say(f"win_runner: could not end the engine: {e}\n")
    try:
        _record_exit(sid, code, "runner_error")
    except Exception as e:     # every kind: this runs while another error is on its way out
        _say(f"win_runner: could not record how the run ended: {e}\n")


def _say(line: str) -> None:
    """Write a line to stderr (runner.log) if it can be written, and carry on if it cannot.

    WHY the failure path may not use a plain write (cross-family review XFA-2): the disk
    being full, or the log gone, may be the very failure being handled, and a write that
    raises there left the engine running. Losing the line is the smaller loss."""
    try:
        sys.stderr.write(line)
    except (OSError, ValueError):      # ValueError: the stream is closed
        return


def main(sid: str) -> int:
    """Run one session to completion; return the engine's exit code (1 when the engine could
    not be started, 143 when the harness ended the run). An error after the engine started
    ends the engine first and is then raised as it was."""
    started = time.monotonic()

    def mark_runner(s: session.Session) -> None:
        s.runner_pid, s.runner_start = os.getpid(), procs.pid_start(os.getpid())
    sess = session.update(sid, mark_runner)
    run = session.run_dir(sess)
    if (run / session.STOP_MARKER).exists():
        # Told to stop before there is an engine (a launch cancelled in its first moments):
        # start none. An engine with no window yet cannot be asked to close.
        _record_exit(sid, None, "stop")
        return ENDED_BY_HARNESS
    env = envsetup.child_env_windows(dict(os.environ), session.prefs_dir(sess))
    try:
        with open(run / "engine.log", "ab") as log:
            # close_fds=False: the engine inherits this process's inheritable handles, which
            # are its own output, the job and the desktop (module docstring).
            engine = subprocess.Popen(launcher.build_argv(sess), cwd=sess.cwd or sess.fs2path,
                                      env=env, stdin=subprocess.DEVNULL, stdout=log,
                                      stderr=subprocess.STDOUT, close_fds=False)
    except OSError as e:           # the engine could not be started: nothing is running
        return _fail(sid, f"engine: {e}")

    def mark_running(s: session.Session) -> None:
        s.engine_pid, s.status = engine.pid, "running"
        s.engine_start = procs.pid_start(engine.pid)
    try:
        session.update(sid, mark_running)
        cause, code = wait_for_end(engine, run / session.STOP_MARKER, sess.runtime_sec,
                                   started)
    except BaseException as e:     # anything at all, Ctrl-C included: the engine is running
        _end_after_failure(sid, engine, e)
        raise
    _record_exit(sid, code, cause)
    if cause is not None or code is None:
        return ENDED_BY_HARNESS
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
