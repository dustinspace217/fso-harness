"""The runner's exit records (QA SL-7, SL-8, TA-16, CR-17 / SF-16).

The SIGTERM tests run the real runner as a child process with a stand-in "engine" (a shell script
that sleeps) and a real Xvfb, as the unit tier: no game data, no scope, bounded waits, and every
process is killed in a finally block.
"""
import contextlib
import os
import socket
import dataclasses
import json
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fsoctl import launcher, runner, session

_REPO = Path(__file__).resolve().parent.parent


def _mk(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                renderer="opengl", width=320, height=240, switches={}, allow_eval=False,
                extra_args=[])
    base.update(kw)
    return session.new_session(**base)


@pytest.fixture
def no_oom(monkeypatch):
    """Pretend the scope's OOM killer never fired (the test process is not in a session scope)."""
    monkeypatch.setattr(runner, "_oom_kills", lambda: 0)


@pytest.mark.parametrize("code, name", [(-9, "SIGKILL"), (-40, "SIG40"), (-64, "SIGRTMAX"),
                                        (0, None), (3, None)])
def test_record_exit_names_every_signal(tmp_runtime, no_oom, code, name):
    # -40 has no name in Python's Signals enum; it used to raise ValueError and skip the record.
    s = _mk(status="running")
    runner._record_exit(s.id, code)
    got = session.load(s.id)
    assert got.status == "exited" and got.ended_by == "exit"
    assert got.exit_info["returncode"] == code and got.exit_info["signal"] == name


def test_record_exit_keeps_a_stopped_status(tmp_runtime, no_oom):
    s = _mk(status="stopped")
    runner._record_exit(s.id, -15)
    got = session.load(s.id)
    assert got.status == "stopped" and got.exit_info["signal"] == "SIGTERM"


def test_record_exit_names_an_oom_kill(tmp_runtime, tmp_path, monkeypatch):
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\noom_group_kill 0\n")
    monkeypatch.setattr(runner, "_memory_events_path", lambda: events)
    s = _mk(status="running")
    runner._record_exit(s.id, -9)
    assert session.load(s.id).ended_by == "oom"


def test_xvfb_start_gives_up_on_a_silent_pipe(monkeypatch):
    # A wedged Xvfb keeps the -displayfd pipe open and writes nothing; the read must time out.
    held = []

    class FakeXvfb:
        """Stands in for Popen: keeps a copy of the pipe's write end open, writes nothing."""
        pid = 0

        def __init__(self, argv, pass_fds=(), **kw):
            held.extend(os.dup(fd) for fd in pass_fds)

        def kill(self):
            """Popen.kill stand-in."""

    monkeypatch.setattr(runner.subprocess, "Popen", FakeXvfb)
    monkeypatch.setattr(runner, "_XVFB_READY_TIMEOUT", 0.5)
    outcome = []

    def attempt():
        try:
            runner._start_xvfb(320, 240)
        except RuntimeError as e:
            outcome.append(str(e))

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(5)
    finished = not t.is_alive()
    for fd in held:
        os.close(fd)   # unblocks a stuck read so the thread can end
    t.join(5)
    assert finished, "Xvfb display read blocked past its timeout"
    assert outcome and "display" in outcome[0]


def _engine_script(tmp_path: Path) -> Path:
    """Write a stand-in engine that ignores its arguments and sleeps (bounded by the test)."""
    script = tmp_path / "fake_engine.sh"
    script.write_text("#!/bin/sh\nexec sleep 120\n")
    script.chmod(0o755)
    return script


def _run_and_terminate(tmp_runtime, sess, marker: bool,
                       expect_xvfb: bool = True) -> tuple[session.Session, bool]:
    """Start the real runner for `sess`, wait until its engine runs, SIGTERM the runner, and
    return (session record, Xvfb was gone before any test cleanup) once the runner has exited.

    The runner gets its own process group (start_new_session), and the finally block kills that
    whole group, so a failed assertion cannot orphan Xvfb or the stand-in (QA TA2-3).
    """
    if marker:
        (session.run_dir(sess) / session.STOP_MARKER).touch()
    env = {**os.environ, "FSO_HARNESS_RUNTIME": str(tmp_runtime), "PYTHONPATH": str(_REPO)}
    proc = subprocess.Popen([sys.executable, "-m", "fsoctl.runner", sess.id], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and session.load(sess.id).status != "running":
            time.sleep(0.1)
        rec = session.load(sess.id)
        assert rec.status == "running", "runner never started the stand-in engine"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 143
        xvfb_pid = session.load(sess.id).xvfb_pid
        xvfb_gone = _gone_within(xvfb_pid, 5) if expect_xvfb else xvfb_pid is None
    finally:
        # Normally nothing is left; ProcessLookupError then is the expected outcome.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    return session.load(sess.id), xvfb_gone


@pytest.fixture
def no_prior_oom():
    """Skip when this test process's cgroup already saw an OOM kill: the runner child inherits
    the cgroup and would (correctly) report "oom" for every SIGTERM (QA TA2-3)."""
    if runner._oom_kills() > 0:
        pytest.skip("this cgroup already recorded an OOM kill")


@pytest.mark.skipif(not Path("/usr/bin/Xvfb").exists(), reason="needs Xvfb")
@pytest.mark.usefixtures("no_prior_oom")
@pytest.mark.parametrize("marker, runtime, cause", [(True, 1800, "stop"),
                                                    (False, 1800, "signal"),
                                                    (False, 5, "runtime_limit")])
def test_sigterm_records_how_the_run_ended(tmp_runtime, tmp_path, marker, runtime, cause):
    # Before: SIGTERM killed the runner outright, so the session said "running" forever with
    # no exit info, and Xvfb's termination was skipped.
    s = _mk(kind="qtfred", binary=str(_engine_script(tmp_path)))
    s.runtime_sec = runtime
    session.save(s)
    got, xvfb_gone = _run_and_terminate(tmp_runtime, s, marker)
    assert got.ended_by == cause
    assert got.status == ("stopped" if cause == "stop" else "exited")
    # The runner passes the polite stop on to the engine (CR2-11); the stand-in obeys it.
    assert got.exit_info and got.exit_info["signal"] == "SIGTERM"
    assert xvfb_gone, "Xvfb outlived its runner"
    xdg = session.run_dir(got) / "xdg-runtime"      # R2: the child's private runtime folder
    assert xdg.is_dir() and stat.S_IMODE(xdg.stat().st_mode) == 0o700


def _gone_within(pid: int, seconds: float) -> bool:
    """Return True once `pid` no longer exists (bounded poll; a zombie is reaped by init)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_a_scope_stopped_after_an_oom_kill_is_recorded_as_oom(tmp_runtime, monkeypatch):
    # The user manager's DefaultOOMPolicy is "stop" (systemctl --user show, 2026-10-12): after
    # the kernel OOM-kills the engine, systemd stops the whole scope, so the runner learns of it
    # through SIGTERM, not through the engine's exit.
    monkeypatch.setattr(runner, "_oom_kills", lambda: 1)
    s = _mk(status="running", runtime_sec=1800)
    assert runner._termination_cause(s.id, time.monotonic()) == "oom"
    (session.run_dir(s) / session.STOP_MARKER).touch()
    assert runner._termination_cause(s.id, time.monotonic()) == "stop"


@pytest.fixture
def sigmask():
    """Restore this thread's signal mask after a test that runs runner code which blocks."""
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    yield
    signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def _sigterm_blocked() -> bool:
    """True if SIGTERM is currently blocked in this thread."""
    return signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, [])


def test_the_exit_record_cannot_be_interrupted_by_sigterm(tmp_runtime, sigmask, monkeypatch):
    # SL2-7: a SIGTERM arriving while the normal-path exit record was written (e.g. a stop right
    # after a clean quit) raised inside it and lost the engine's real exit code.
    s = _mk(status="running")

    class Engine:
        """Stand-in Popen: the engine has already exited with code 0."""
        pid = 4242

        def __init__(self, *a, **kw):
            pass

        def wait(self, timeout=None):
            return 0
    seen = []
    monkeypatch.setattr(runner.subprocess, "Popen", Engine)
    monkeypatch.setattr(runner.launcher, "build_argv", lambda sess: ["engine"])
    monkeypatch.setattr(runner, "_record_exit", lambda *a, **k: seen.append(_sigterm_blocked()))
    assert runner._run_engine(s.id, {}, time.monotonic()) == 0
    assert seen == [True]


def test_xvfb_is_terminated_with_sigterm_blocked(tmp_runtime, sigmask, monkeypatch):
    # SL2-7: a SIGTERM inside main's finally used to skip xvfb.terminate() with a traceback.
    s = _mk()
    seen = []

    class Xvfb:
        """Stand-in for the Xvfb Popen."""
        pid = 4343

        def terminate(self):
            seen.append(_sigterm_blocked())
    monkeypatch.setattr(runner, "_start_xvfb", lambda w, h: (":9", Xvfb()))
    monkeypatch.setattr(runner, "_run_engine", lambda sid, env, started: 0)
    assert runner.main(s.id) == 0
    assert seen == [True]
    signal.signal(signal.SIGTERM, signal.SIG_DFL)     # main() installed its handler


def test_reap_passes_the_stop_on_and_waits_twenty_seconds(monkeypatch):
    # CR2-11: the runner SIGKILLed the engine 3 s into a polite stop, so every stop recorded
    # SIGKILL and cut a debug build's shutdown logging. It now SIGTERMs the engine itself (the
    # runner alone may have been signalled) and allows 20 s, the same as scope.stop.
    calls = []

    class Engine:
        """Stand-in engine that ignores SIGTERM until killed."""
        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            if "kill" not in calls:
                raise subprocess.TimeoutExpired("engine", timeout)
            return -9
    assert runner._reap(Engine()) == -9
    assert calls[:3] == ["terminate", ("wait", 20.0), "kill"]


def test_reap_gives_up_on_an_engine_that_survives_sigkill():
    # YK-4: an engine stuck in the kernel (D state) outlives SIGKILL for a while; the second
    # wait's TimeoutExpired escaped, so the exit was never recorded and the session stayed
    # "running". Now the code is unknown (None) and the record is still written.
    class Engine:
        """Stand-in engine that never exits."""
        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("engine", timeout)
    assert runner._reap(Engine()) is None


@pytest.mark.skipif(not Path("/usr/bin/Xvfb").exists(), reason="needs Xvfb")
@pytest.mark.usefixtures("no_prior_oom")
def test_the_runner_ends_the_run_itself_at_its_runtime_limit(tmp_runtime, tmp_path):
    # QA SL-1 (probed: an expiring RuntimeMaxSec took Xvfb down with the engine -> SIGABRT core).
    # Now the runner reaps the engine at runtime_sec and ends Xvfb after it, with no SIGTERM.
    s = _mk(kind="qtfred", binary=str(_engine_script(tmp_path)))
    s.runtime_sec = 3
    session.save(s)
    env = {**os.environ, "FSO_HARNESS_RUNTIME": str(tmp_runtime), "PYTHONPATH": str(_REPO)}
    proc = subprocess.Popen([sys.executable, "-m", "fsoctl.runner", s.id], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        assert proc.wait(timeout=30) == 143
        got = session.load(s.id)
        assert got.ended_by == "runtime_limit" and got.status == "exited"
        assert got.exit_info["signal"] == "SIGTERM"      # the stand-in got the polite stop
        assert _gone_within(got.xvfb_pid, 5), "Xvfb outlived its runner"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def test_a_stop_while_the_engine_registers_still_reaps_it(tmp_runtime, sigmask, monkeypatch):
    # QA SL-3: the try that catches _Terminated used to open after the engine was registered, so
    # a stop landing during registration skipped _reap and main ended Xvfb under a live engine.
    s = _mk(status="starting")

    class Engine:
        """Stand-in Popen."""
        pid = 4242

        def __init__(self, *a, **kw):
            pass

        def wait(self, timeout=None):
            return 0
    real_update = runner.session.update

    def update(sid, fn):
        if fn.__name__ == "mark_running":
            raise runner._Terminated()
        return real_update(sid, fn)
    reaped = []
    monkeypatch.setattr(runner.subprocess, "Popen", Engine)
    monkeypatch.setattr(runner.launcher, "build_argv", lambda sess: ["engine"])
    monkeypatch.setattr(runner.session, "update", update)
    monkeypatch.setattr(runner, "_reap", lambda engine: reaped.append(engine.pid) or -15)
    monkeypatch.setattr(runner, "_record_exit", lambda *a, **k: None)
    with pytest.raises(runner._Terminated):
        runner._run_engine(s.id, {}, time.monotonic())
    assert reaped == [4242]


@pytest.mark.parametrize("runtime, elapsed, want", [(None, 5, None), (10, 4, 6), (10, 12, 0.1)])
def test_time_left_is_bounded_below(tmp_runtime, runtime, elapsed, want, monkeypatch):
    s = _mk()
    s.runtime_sec = runtime
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100.0 + elapsed)
    got = runner._time_left(s, 100.0)
    assert got == (pytest.approx(want) if want is not None else None)


def test_a_stop_at_the_runtime_limit_still_reaps_the_engine(tmp_runtime, sigmask, monkeypatch):
    # QA XFA-1/XFK-1: a SIGTERM landing between the runtime-limit timeout and the signal block
    # used to escape the TimeoutExpired handler unreaped. It is now caught there, and the stop
    # (its marker present) is recorded as the cause.
    s = _mk(status="running")
    (session.run_dir(s) / session.STOP_MARKER).touch()

    class Engine:
        """Stand-in Popen whose wait always reaches the runtime limit."""
        pid = 4242

        def __init__(self, *a, **kw):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("engine", timeout)
    calls = {"block": 0}
    real_block = runner._block_sigterm

    def block():
        calls["block"] += 1
        if calls["block"] == 1:
            raise runner._Terminated()      # the stop arrives in the gap
        real_block()
    reaped, recorded = [], []
    monkeypatch.setattr(runner.subprocess, "Popen", Engine)
    monkeypatch.setattr(runner.launcher, "build_argv", lambda sess: ["engine"])
    monkeypatch.setattr(runner, "_block_sigterm", block)
    monkeypatch.setattr(runner, "_reap", lambda engine: reaped.append(engine.pid) or -15)
    monkeypatch.setattr(runner, "_record_exit",
                        lambda sid, code, cause=None: recorded.append(cause))
    monkeypatch.setattr(runner, "_oom_kills", lambda: 0)
    assert runner._run_engine(s.id, {}, time.monotonic()) == 143
    assert reaped == [4242] and recorded == ["stop"]



@pytest.mark.usefixtures("no_prior_oom")
def test_a_standalone_session_runs_without_xvfb_or_display(tmp_runtime, tmp_path):
    # Plan Task 6: a standalone multiplayer server has no window, so the runner starts no Xvfb
    # and the engine gets no DISPLAY at all (a stray one would reach the user's desktop).
    script = tmp_path / "fake_engine.sh"
    script.write_text("#!/bin/sh\nenv > \"$FSO_PREFERENCES_PATH/env.txt\"\nexec sleep 120\n")
    script.chmod(0o755)
    s = _mk(binary=str(script), role="standalone", port=7808)   # no match record: own network
    got, _ = _run_and_terminate(tmp_runtime, s, marker=True, expect_xvfb=False)
    assert got.display is None and got.xvfb_pid is None
    env_text = (session.prefs_dir(got) / "env.txt").read_text()
    assert "\nDISPLAY=" not in env_text and not env_text.startswith("DISPLAY=")
    assert got.ended_by == "stop"


# ---- the ENGINE runs in the namespace; the runner and Xvfb stay on the host ---------------------

def _ns_sess(**kw):
    return _mk(**kw) if "status" in kw else _mk(status="starting", **kw)


def test_engine_argv_wraps_only_the_engine(tmp_runtime, monkeypatch):
    """Private: the unshare prefix in front of the engine's own argv; real: bare; match: the
    match's nsenter prefix; an old record (network None) is treated as private."""
    built_for = []
    monkeypatch.setattr(launcher, "build_argv",
                        lambda sess: built_for.append(sess.id) or ["fs2", "-nomovies"])
    wrapped = [*launcher.PRIVATE_NET_PREFIX, "fs2", "-nomovies"]
    private = _ns_sess(network="private")
    assert runner.engine_argv(private) == wrapped
    assert built_for == [private.id]                     # the argv is built for THIS session
    assert runner.engine_argv(_ns_sess(network=None)) == wrapped
    assert runner.engine_argv(_ns_sess(network="real")) == ["fs2", "-nomovies"]
    entered = []
    monkeypatch.setattr(runner.match, "load", lambda mid: entered.append(mid) or object())
    monkeypatch.setattr(runner.match, "nsenter_prefix", lambda m: ["nsenter", "--"])
    got = runner.engine_argv(_ns_sess(match="m0a1b2c3d", role="client"))
    assert got == ["nsenter", "--", "fs2", "-nomovies"]
    assert entered == ["m0a1b2c3d"]


def test_private_prefix_script_passes_argv_intact_and_stops_when_ip_fails(tmp_path):
    """Executable check of the sh script (QA TA-2): every word reaches the program unchanged
    (spaces, quotes), and a failing `ip` stops the chain instead of exec'ing with lo down."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ip = fake_bin / "ip"
    ip.write_text("#!/bin/sh\nexit ${FAKE_IP_EXIT:-0}\n")
    ip.chmod(0o755)
    script = launcher.PRIVATE_NET_PREFIX[-2]                      # the sh -c program text
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    out = subprocess.run(["sh", "-c", script, "sh", sys.executable, "-c",
                          "import sys, json; print(json.dumps(sys.argv[1:]))", "a b", '"q"'],
                         env=env, capture_output=True, text=True, timeout=20, check=False)
    assert out.returncode == 0 and json.loads(out.stdout) == ["a b", '"q"']
    out = subprocess.run(["sh", "-c", script, "sh", sys.executable, "-c", "print('ran')"],
                         env={**env, "FAKE_IP_EXIT": "1"}, capture_output=True, text=True,
                         timeout=20, check=False)
    assert out.returncode != 0 and "ran" not in out.stdout


@pytest.mark.skipif(not Path("/usr/bin/Xvfb").exists(), reason="needs Xvfb")
def test_host_xvfb_skips_a_number_whose_abstract_socket_is_taken():
    """A decoy abstract listener on the lowest host-free number must make Xvfb's own -displayfd
    allocation skip it: the property that makes host-side Xvfb safe where a namespaced one was
    not (QA SL-1 / SA-1 / XFA-1 / TA-1)."""
    def host_free(n):
        if Path(f"/tmp/.X{n}-lock").exists() or Path(f"/tmp/.X11-unix/X{n}").exists():
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(b"\0/tmp/.X11-unix/X%d" % n)
            return False
        except OSError:
            return True
        finally:
            s.close()
    m = next(n for n in range(1, 200) if host_free(n))           # bounded
    decoy = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    decoy.bind(b"\0/tmp/.X11-unix/X%d" % m)
    decoy.listen(1)
    try:
        display, proc = runner._start_xvfb(320, 240)
        try:
            assert display != f":{m}", "Xvfb took the decoy's number"
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    finally:
        decoy.close()


def test_runner_fails_the_record_when_the_match_prefix_cannot_be_built(tmp_runtime):
    """A match session whose match record is gone by the time the runner starts (the match was
    removed between launch and runner start) must end as a FAILED record naming the match, not a
    traceback with the record stuck at "starting" (QA Phase B, CR hazard 1: match.nsenter_prefix
    raises HarnessError, which the runner used to let through)."""
    s = _mk(binary="/bin/true", match="m0a1b2c3d", role="client", network="private")
    env = {**os.environ, "FSO_HARNESS_RUNTIME": str(tmp_runtime), "PYTHONPATH": str(_REPO)}
    proc = subprocess.run([sys.executable, "-m", "fsoctl.runner", s.id], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    rec = session.load(s.id)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr, proc.stderr[-400:]
    assert rec.status == "failed", rec.status
    assert "m0a1b2c3d" in json.dumps(dataclasses.asdict(rec))
