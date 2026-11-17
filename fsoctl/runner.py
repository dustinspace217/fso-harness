"""The process that runs INSIDE a session's scope: Xvfb, then the engine or qtFRED.

WHY a runner process: the scope's main process must outlive `fsoctl launch` (which returns as soon
as the session is ready) and must own both Xvfb and the engine, so stopping the scope ends both.
It also records how the engine exited, which crash_report needs.

Started by launcher.launch as `python -m fsoctl.runner <session id>` inside the named scope.

How a run ends is recorded in session.ended_by (QA SL-7): when systemd ends the scope (fsoctl
stop, RuntimeMaxSec, or anyone running `systemctl stop`) it SIGTERMs every process in it, the
runner included; the runner's SIGTERM handler makes sure that end is written down instead of the
session staying "running" forever.
"""
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import envsetup, launcher, match, sandbox, session, sound
from .errors import EXIT_LAUNCH, HarnessError

_XVFB_READY_TIMEOUT = 10.0
_XVFB_EXTRA_HEIGHT = 80  # room below the game window for qtFRED's status bar and dialogs
_RUNTIME_SLACK = 5       # a SIGTERM this close to the runtime limit is the runtime limit
# How long a terminated runner waits for the engine's own exit before SIGKILL: the same 20 s
# scope.stop allows a polite stop, so a debug build can finish its shutdown logging (CR2-11).
_REAP_TIMEOUT = 20.0


class _Terminated(Exception):
    """Raised in the runner's main thread by the SIGTERM handler."""


def _on_sigterm(signum: int, frame: object) -> None:
    """SIGTERM handler: ignore further SIGTERMs, then unwind main() so the end gets recorded.

    WHY raise instead of recording here: the handler can interrupt main() while it holds
    session.update's flock, and a second flock on a fresh open of the same file from this same
    process would block forever. Raising first leaves that `with` block (releasing the lock).
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise _Terminated()


def _block_sigterm() -> None:
    """Block SIGTERM in this thread for the rest of the runner's life (QA SL2-7).

    Used once the engine has exited and in main's finally: from there on the runner only writes
    its record and exits, and a SIGTERM raised mid-write lost the engine's real exit code or
    skipped xvfb.terminate(). A blocked signal stays pending and is dropped when the runner
    exits. WHY not block for the whole run: children inherit the signal mask (probed: a Popen
    child of a SIGTERM-blocking parent shows it in SigBlk), so Xvfb and the engine would ignore
    the scope's polite stop. Nothing is started after this is called.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})


def _read_display(rfd: int, timeout: float) -> bytes:
    """Read Xvfb's display number line from the -displayfd pipe, giving up after `timeout` s.

    Uses select() so a wedged Xvfb (pipe open, nothing written) cannot block the runner: a plain
    os.read() would wait forever (QA CR-17 / SF-16). Returns what was read, possibly b"".
    """
    deadline = time.monotonic() + timeout
    data = b""
    while not data.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        ready, _, _ = select.select([rfd], [], [], left)
        if not ready:
            break
        chunk = os.read(rfd, 16)
        if not chunk:
            break
        data += chunk
    return data


def _start_xvfb(width: int, height: int) -> tuple[str, subprocess.Popen]:
    """Start Xvfb on a free display and return (":N", process).

    Xvfb chooses the display number itself and writes it to the pipe given by -displayfd, so
    parallel sessions never collide on a fixed number. It runs HERE, on the host, never inside
    the session's network namespace (see launcher.PRIVATE_NET_PREFIX for why).
    """
    rfd, wfd = os.pipe()
    argv = ["Xvfb", "-displayfd", str(wfd), "-screen", "0",
            f"{width}x{height + _XVFB_EXTRA_HEIGHT}x24", "-nolisten", "tcp"]
    proc = subprocess.Popen(argv, pass_fds=(wfd,), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    os.close(wfd)
    try:
        data = _read_display(rfd, _XVFB_READY_TIMEOUT)
    finally:
        os.close(rfd)
    if not data.strip():
        proc.kill()
        raise RuntimeError(f"Xvfb did not report a display within {_XVFB_READY_TIMEOUT:.0f}s")
    return f":{data.strip().decode()}", proc


def _signal_name(code: int) -> str | None:
    """Return the signal name for a negative Popen return code, None for a normal exit.

    Real-time signals without a name in Python's enum (e.g. 40) become "SIG40" instead of raising
    ValueError, which used to skip recording the exit entirely (QA TA-16).
    """
    if code >= 0:
        return None
    try:
        return signal.Signals(-code).name
    except ValueError:
        return f"SIG{-code}"


def _memory_events_path() -> Path:
    """Return this process's cgroup memory.events file (the scope's, since we run inside it)."""
    rel = Path("/proc/self/cgroup").read_text(encoding="utf-8").strip().split("::", 1)[-1]
    return Path("/sys/fs/cgroup") / rel.lstrip("/") / "memory.events"


def _oom_kills() -> int:
    """Return the scope cgroup's oom_kill count (0 when the file cannot be read)."""
    try:
        text = _memory_events_path().read_text(encoding="utf-8")
    except OSError:
        return 0    # no cgroup v2 memory controller here: we cannot tell, so report no OOM
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            return int(value)
    return 0


def _record_exit(sid: str, code: int | None, ended_by: str | None = None) -> None:
    """Store how the run ended: the engine's exit code/signal (if known) and ended_by.

    Receives: code (Popen return code, negative = signal; None if the engine never ran or did not
    exit), ended_by (None = the engine exited by itself: "oom" if the scope's OOM killer fired,
    else "exit"). A status of "stopped" is kept; otherwise the status becomes "exited", or
    "stopped" when ended_by is "stop".
    """
    if ended_by is None:
        ended_by = "oom" if _oom_kills() > 0 else "exit"

    def apply(sess: session.Session) -> None:
        if ended_by == "stop":
            sess.status = "stopped"
        elif sess.status != "stopped":
            sess.status = "exited"
        if code is not None:
            sess.exit_info = {"returncode": code, "signal": _signal_name(code), "at": time.time()}
        if sess.ended_by is None:
            sess.ended_by = ended_by
    session.update(sid, apply)


def _termination_cause(sid: str, started: float) -> str:
    """Say why the scope was ended: "stop" (fsoctl stop), "oom", "runtime_limit", or "signal".

    "oom" comes through here too: the user manager's DefaultOOMPolicy is "stop" (checked with
    `systemctl --user show`, 2026-10-12), so after the kernel OOM-kills a process in the scope,
    systemd stops the whole scope and the runner sees SIGTERM.
    """
    sess = session.load(sid)
    if (session.run_dir(sess) / session.STOP_MARKER).exists():
        return "stop"
    if _oom_kills() > 0:
        return "oom"
    if sess.runtime_sec and time.monotonic() - started >= sess.runtime_sec - _RUNTIME_SLACK:
        return "runtime_limit"
    return "signal"


def _reap(engine: subprocess.Popen) -> int | None:
    """Pass the stop on to the engine (SIGTERM), wait up to _REAP_TIMEOUT s, then SIGKILL it.

    Returns the engine's exit code, or None if it outlives SIGKILL too (stuck in the kernel, "D"
    state): the exit is then recorded without a code instead of the runner dying with the
    session left "running" (QA YK-4). The SIGTERM is sent here too because the runner alone may
    have been signalled; when systemd stops the scope the engine already has one, and a second
    is harmless (SDL turns it into a quit event).
    """
    engine.terminate()
    try:
        return engine.wait(timeout=_REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        engine.kill()
    try:
        return engine.wait(timeout=_REAP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None


def engine_argv(sess: session.Session, env: dict | None = None,
                info_fd: int | None = None, seccomp_fd: int | None = None) -> list[str]:
    """Return the engine's argv with its network prefix: the match's prefix for a match session
    (nsenter into a private match's namespace, nothing for a real match), the bwrap prefix for a
    sandbox session (sandbox.argv; needs the child `env`, bwrap's `info_fd` and `seccomp_fd`),
    PRIVATE_NET_PREFIX
    for a private ordinary session (an old record with no network field counts as private), bare
    for a real one. Only the engine is wrapped: this runner and its Xvfb stay on the host."""
    argv = launcher.build_argv(sess)
    if sess.match:
        return match.nsenter_prefix(match.load(sess.match)) + argv
    if sess.network == "real":
        return argv
    if sess.network == "sandbox":
        if env is None or info_fd is None or seccomp_fd is None:
            # Never fall through to the private prefix: that would run an untrusted build with
            # the whole filesystem in reach while the record says "sandbox".
            raise HarnessError(EXIT_LAUNCH, "a sandbox session needs its environment and info fd",
                               "internal error: report it")
        return sandbox.argv(sess, env, info_fd, seccomp_fd) + argv
    return launcher.PRIVATE_NET_PREFIX + argv


def _popen_engine(sess: session.Session, env: dict, log) -> subprocess.Popen | sandbox.Engine:
    """Start the engine (wrapped for its network) writing to `log`; return its process handle.

    For a sandbox session the handle is a sandbox.Engine around bwrap whose pid is the engine's
    HOST pid (the one /proc/<pid>/maps and coredumpctl need) and which signals it through a pidfd,
    found through bwrap's --info-fd pipe. EXIT_LAUNCH is raised (bwrap killed first if it still
    runs) when bwrap ended without reporting a pid (the sandbox itself could not be set up; this
    is recorded by the runner, not guessed from log text) or when the sandbox runs but its engine
    was not found; each of the two waits is bounded by sandbox.START_TIMEOUT. If the engine
    started and already ended, the handle carries pid None and the caller's wait() records that.
    """
    if sess.network != "sandbox":
        return subprocess.Popen(engine_argv(sess), cwd=sess.cwd or sess.fs2path, env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    rfd, wfd = os.pipe()
    filter_r, filter_w = os.pipe()     # the seccomp program: 72 bytes, far below a pipe's buffer
    try:
        os.write(filter_w, sandbox.seccomp_filter())
        os.close(filter_w)
        proc = subprocess.Popen(engine_argv(sess, env, wfd, filter_r), env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                pass_fds=(wfd, filter_r))
    finally:
        os.close(wfd)                 # bwrap holds its own copy; EOF must come from bwrap
        os.close(filter_r)
    # A pidfd on bwrap for the watchdog's kill (QA TA4-3), opened here, before anything below
    # can poll() or wait() bwrap: until it is reaped its pid cannot be reused, so the pidfd is
    # bwrap's for certain (an open after a reap could name another process).
    bwrap_pidfd: int | None = None
    try:
        bwrap_pidfd = sandbox._open_pidfd(proc.pid)   # a failure here still ends bwrap below
        init = sandbox.read_info(rfd, sandbox.START_TIMEOUT)
        if init is None:
            raise HarnessError(EXIT_LAUNCH, "the sandbox did not start (bwrap reported no "
                               "process)", "see `fsoctl log <id>`: engine.log holds bwrap's "
                               "messages; check that unprivileged user namespaces are allowed")
        found = sandbox.find_engine(init, lambda: proc.poll() is None, sandbox.START_TIMEOUT)
        if found is None and proc.poll() is None:
            raise HarnessError(EXIT_LAUNCH, "the sandbox is running but its engine was not found",
                               "see `fsoctl log <id>` (engine.log holds bwrap's own messages)")
    except BaseException:
        # Includes _Terminated from a stop landing here: the caller never got the handle, so the
        # sandbox is ended now instead of living on until the runner exits.
        proc.kill()
        if bwrap_pidfd is not None:
            os.close(bwrap_pidfd)
        raise
    finally:
        os.close(rfd)
    return sandbox.Engine(proc, *(found or (None, None)), bwrap_pidfd)


def _run_engine(sid: str, env: dict, started: float) -> int:
    """Start the engine/qtFRED, wait for it, record its exit, and return its exit code.

    On SIGTERM (_Terminated) it records the cause and the engine's code, then re-raises.
    """
    sess = session.load(sid)
    with open(session.run_dir(sess) / "engine.log", "ab") as log:
        def mark_running(s: session.Session) -> None:
            s.engine_pid, s.status = proc.pid, "running"
            if s.network == "sandbox":
                s.window_pid = sandbox.ENGINE_NS_PID     # SDL's _NET_WM_PID inside the sandbox
        # The try opens BEFORE Popen, so the engine's registration is covered by the handler that
        # reaps it: a stop landing there used to skip _reap and main ended Xvfb under a live
        # engine (QA SL-3, XFA-1/XFK-1). Still unguarded: a stop inside Popen itself (while it
        # forks) or before its handle is stored in `proc`, within a second of launch; no harness
        # path stops a session at that moment.
        proc: subprocess.Popen | sandbox.Engine | None = None
        dog: sandbox.Watchdog | None = None
        # The prefs disk's free space before the engine writes anything: the watchdog's
        # baseline for the free-space-drop check (QA SA4-1/AT4-2).
        baseline = (sandbox.free_bytes(session.prefs_dir(sess)) if sess.network == "sandbox"
                    else None)
        try:
            proc = _popen_engine(sess, env, log)
            session.update(sid, mark_running)
            dog = _watch(sess, proc, baseline)
            code = proc.wait(timeout=_time_left(sess, started))
            _block_sigterm()      # the run is over; nothing may interrupt the record (SL2-7)
        except _Terminated:
            _record_exit(sid, _reap(proc) if proc is not None else None,
                         _termination_cause(sid, started))
            raise
        except subprocess.TimeoutExpired:
            # The runner's own runtime limit (QA SL-1): end the engine first and Xvfb last (main's
            # finally), instead of letting RuntimeMaxSec take both down at once, which aborted the
            # engine with a core dump.
            cause = "runtime_limit"
            try:
                _block_sigterm()
            except _Terminated:   # a stop landed just before the block (XFA-1/XFK-1); the
                _block_sigterm()  # handler ignores any later SIGTERM, so one catch is enough
                cause = _termination_cause(sid, started)
            _record_exit(sid, _reap(proc), cause)
            return 143            # same as the other harness-ended runs (main's docstring)
        finally:
            if dog is not None:
                dog.stop()
                # A trip kills the sandbox and then empties prefs from the watchdog's thread;
                # the runner waits for that (bounded) instead of exiting under it.
                dog.join(sandbox.WATCHDOG_JOIN)
    _record_exit(sid, code, "disk_limit" if dog is not None and dog.tripped else None)
    return code


def _watch(sess: session.Session, proc, baseline: int | None = None) -> sandbox.Watchdog | None:
    """Start and return the prefs-size watchdog for a sandbox session (None for any other);
    baseline is the prefs disk's free bytes before the engine started."""
    if sess.network != "sandbox" or not isinstance(proc, sandbox.Engine):
        return None
    dog = sandbox.Watchdog(sess.id, session.prefs_dir(sess), proc, baseline)
    dog.start()
    return dog


def _time_left(sess: session.Session, started: float) -> float | None:
    """Seconds until the session's runtime limit (at least 0.1), or None when it has none."""
    if not sess.runtime_sec:
        return None
    return max(0.1, sess.runtime_sec - (time.monotonic() - started))


def _fail(sid: str, error: str) -> int:
    """Mark the session failed with `error` and return the runner's failure code (1)."""
    def apply(s: session.Session) -> None:
        s.status, s.exit_info = "failed", {"error": error, "at": time.time()}
    session.update(sid, apply)
    return 1


def make_first_oom_victim() -> int:
    """Raise this process's oom_score_adj to the maximum and return the value read back.

    Every child (Xvfb, the engine) inherits it, so a global out-of-memory takes the session before a
    desktop application. Done here and not as a scope property: systemd rejects OOMScoreAdjust on a
    transient scope ("Unknown assignment", 2026-10-16), and a process may raise its own score
    without privilege.
    """
    with open("/proc/self/oom_score_adj", "w", encoding="utf-8") as f:
        f.write("1000")
    with open("/proc/self/oom_score_adj", encoding="utf-8") as f:
        return int(f.read())


def main(sid: str) -> int:
    """Run one session to completion; return the engine's exit code (1 on runner failure, 143
    when the scope was ended). Xvfb is terminated on every path (QA SL-8)."""
    started = time.monotonic()
    signal.signal(signal.SIGTERM, _on_sigterm)
    make_first_oom_victim()
    xvfb = None
    try:
        sess = session.update(sid, lambda s: setattr(s, "runner_pid", os.getpid()))
        display = None
        if sess.role != "standalone":
            # A standalone server has no window (stand_gui-unix.cpp); it gets no display at all,
            # so a stray SDL init cannot open one anywhere.
            try:
                display, xvfb = _start_xvfb(sess.width, sess.height)
            except (OSError, RuntimeError) as e:
                return _fail(sid, f"xvfb: {e}")
        xvfb_pid = xvfb.pid if xvfb else None

        def mark_display(s: session.Session) -> None:
            s.display, s.xvfb_pid = display, xvfb_pid
        session.update(sid, mark_display)
        run = session.run_dir(sess)
        (run / "home").mkdir(exist_ok=True)
        (run / "xdg-runtime").mkdir(mode=0o700, exist_ok=True)   # XDG requires 0700
        env = envsetup.for_kind(envsetup.child_env(dict(os.environ), display,
                                                   session.prefs_dir(sess), sess.renderer, run),
                                sess.kind)
        if sess.sound:
            env.update(sound.engine_env(sess.id))
        if sess.network == "sandbox":
            # Folder variables point inside the sandbox, whose own tmpfs folders hold them.
            sandbox.sandbox_env(env)
            sandbox.write_identity(run)
        else:
            Path(env["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
        try:
            return _run_engine(sid, env, started)
        except OSError as e:
            return _fail(sid, f"engine: {e}")
        except HarnessError as e:
            # engine_argv's prefix lookup: the match was removed between launch and this start
            # (QA Phase B, CR hazard 1); a failed record beats a traceback stuck at "starting".
            return _fail(sid, f"engine: {e.message}")
    except _Terminated:
        # Ended before (or while) the engine was being started: record why, without exit code.
        if session.load(sid).ended_by is None:
            _record_exit(sid, None, _termination_cause(sid, started))
        return 143
    finally:
        _block_sigterm()
        if xvfb is not None:
            xvfb.terminate()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
