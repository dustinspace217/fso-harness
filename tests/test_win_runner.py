"""The Windows runner (cross-platform Plan B, Task 5): the process that runs inside a
session's job, starts the engine, and ends it on a stop request or at the runtime limit.

No real engine and no real time here: the engine is a stand-in with poll/wait/kill, the clock
and the sleep are passed in. tests/winlive covers the real thing on Windows."""
import os
import subprocess

import pytest

from fsoctl import envsetup, session
from fsoctl.platform import win_runner


class FakeEngine:
    """A process stand-in: `exits_after` polls it reports `code`; `closes` says whether a
    close request ends it (within the wait); kill always ends it."""
    pid = 4242

    def __init__(self, exits_after=None, code=0, closes=True, dies=True):
        self.exits_after, self.code, self.closes, self.dies = exits_after, code, closes, dies
        self.polls, self.killed, self.waits, self.done = 0, False, [], None

    def poll(self):
        self.polls += 1
        if self.done is None and self.exits_after is not None and self.polls > self.exits_after:
            self.done = self.code
        return self.done

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.done is None:
            raise subprocess.TimeoutExpired("engine", timeout)
        return self.done

    def kill(self):
        self.killed = True
        if self.dies:
            self.done = 1


class Clock:
    """A clock that only moves when the runner sleeps."""

    def __init__(self):
        self.now, self.sleeps = 100.0, 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds == win_runner.RUNNER_POLL_S
        self.now += seconds
        self.sleeps += 1


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(win_runner.time, "monotonic", c.monotonic)
    monkeypatch.setattr(win_runner.time, "sleep", c.sleep)
    return c


def closer(engine, record):
    """A stand-in for win_runner._ask_close: records the call; ends the engine if it closes."""
    def ask(pid):
        record.append(pid)
        if engine.closes:
            engine.done = 0
        return 1
    return ask


def test_the_engine_exiting_by_itself_is_not_a_stop(tmp_path, clock):
    engine = FakeEngine(exits_after=3, code=7)
    assert win_runner.wait_for_end(engine, tmp_path / "stop", 60, clock.now) == (None, 7)
    assert clock.sleeps == 3 and not engine.killed


def test_runner_stops_on_the_stop_file(tmp_path, clock, monkeypatch):
    engine, asked = FakeEngine(), []
    monkeypatch.setattr(win_runner, "_ask_close", closer(engine, asked))
    stop = tmp_path / "stop"
    real_sleep = clock.sleep

    def sleep(seconds):
        real_sleep(seconds)
        if clock.sleeps == 2:
            stop.touch()                               # `fsoctl stop` asks
    monkeypatch.setattr(win_runner.time, "sleep", sleep)
    assert win_runner.wait_for_end(engine, stop, 60, clock.now) == ("stop", 0)
    assert asked == [4242] and not engine.killed
    assert engine.waits == [win_runner.ENGINE_CLOSE_WAIT_S]


def test_runner_ends_the_engine_at_runtime_sec(tmp_path, clock, monkeypatch):
    engine, asked = FakeEngine(), []
    monkeypatch.setattr(win_runner, "_ask_close", closer(engine, asked))
    started = clock.now
    assert win_runner.wait_for_end(engine, tmp_path / "stop", 3, started) == ("runtime_limit", 0)
    assert clock.now - started == pytest.approx(3.0) and asked == [4242]


def test_an_engine_that_ignores_the_close_request_is_ended(tmp_path, clock, monkeypatch):
    engine, asked = FakeEngine(closes=False), []
    monkeypatch.setattr(win_runner, "_ask_close", closer(engine, asked))
    (tmp_path / "stop").touch()
    assert win_runner.wait_for_end(engine, tmp_path / "stop", 60, clock.now) == ("stop", 1)
    assert engine.killed and engine.waits == [win_runner.ENGINE_CLOSE_WAIT_S,
                                              win_runner.ENGINE_KILL_WAIT_S]


def test_an_engine_that_cannot_be_ended_is_recorded_without_a_code(tmp_path, clock, monkeypatch):
    engine = FakeEngine(closes=False, dies=False)
    monkeypatch.setattr(win_runner, "_ask_close", closer(engine, []))
    (tmp_path / "stop").touch()
    assert win_runner.wait_for_end(engine, tmp_path / "stop", 60, clock.now) == ("stop", None)


def _session(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[], runtime_sec=60)
    base.update(kw)
    return session.new_session(**base)


def run_main(monkeypatch, sess, cause, code):
    """Run win_runner.main for `sess` with the engine start and the wait replaced; return
    (main's result, the keyword arguments the engine was started with)."""
    started = {}

    def popen(argv, **kw):
        started.update(argv=argv, **kw)
        return FakeEngine()
    monkeypatch.setattr(win_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(win_runner, "wait_for_end", lambda *a: (cause, code))
    monkeypatch.setattr(win_runner.procs, "pid_start", lambda pid: 111)
    return win_runner.main(sess.id), started


@pytest.mark.parametrize("case", [
    # (cause, engine's code) -> (status, ended_by, the runner's own result)
    ((None, 0), ("exited", "exit", 0)),
    ((None, 3), ("exited", "exit", 3)),
    (("stop", 0), ("stopped", "stop", 143)),
    (("runtime_limit", 1), ("exited", "runtime_limit", 143)),
    (("stop", None), ("stopped", "stop", 143)),
])
def test_runner_records_exit_code_and_cause(tmp_runtime, monkeypatch, case):
    (cause, code), (status, ended_by, result) = case
    s = _session()
    got, _ = run_main(monkeypatch, s, cause, code)
    rec = session.load(s.id)
    assert got == result
    assert (rec.status, rec.ended_by) == (status, ended_by)
    if code is None:
        assert rec.exit_info is None
    else:
        assert rec.exit_info["returncode"] == code and rec.exit_info["signal"] is None
    assert rec.engine_pid == 4242 and rec.runner_pid and rec.runner_start == 111


def test_the_engine_inherits_the_runners_handles_and_gets_the_session_environment(
        tmp_runtime, monkeypatch):
    s = _session(fs2path="D:\\My Games\\FS2", binary="C:\\Fs2 Builds\\fs2 open.exe")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\José Müller")
    _, started = run_main(monkeypatch, s, None, 0)
    # close_fds False: the engine inherits the job handle, which keeps the job findable by
    # name for as long as the engine lives (win_scope, probe 10)
    assert started["close_fds"] is False
    assert started["cwd"] == "D:\\My Games\\FS2"
    assert started["argv"][0] == "C:\\Fs2 Builds\\fs2 open.exe"
    child = started["env"]
    assert child["FSO_PREFERENCES_PATH"] == str(session.prefs_dir(s)).rstrip("\\/") + "\\"
    assert child["USERPROFILE"] == "C:\\Users\\José Müller"
    assert started["stdin"] == subprocess.DEVNULL and started["stderr"] == subprocess.STDOUT


def test_a_runner_that_cannot_start_the_engine_marks_the_session_failed(tmp_runtime, monkeypatch):
    s = _session()

    def popen(argv, **kw):
        raise FileNotFoundError(2, "The system cannot find the file specified")
    monkeypatch.setattr(win_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(win_runner.procs, "pid_start", lambda pid: 111)
    assert win_runner.main(s.id) == 1
    rec = session.load(s.id)
    assert rec.status == "failed" and "cannot find the file" in rec.exit_info["error"]


def test_windows_child_env_sets_only_the_prefs_path(tmp_path):
    base = {"PATH": "C:\\Windows", "USERPROFILE": "C:\\Users\\x", "DISPLAY": ":0"}
    child = envsetup.child_env_windows(base, tmp_path / "prefs")
    assert child == {**base, "FSO_PREFERENCES_PATH": str(tmp_path / "prefs") + "\\"}
    assert base == {"PATH": "C:\\Windows", "USERPROFILE": "C:\\Users\\x", "DISPLAY": ":0"}
    for added in ("HOME", "XDG_RUNTIME_DIR", "SDL_VIDEODRIVER", "LIBGL_ALWAYS_SOFTWARE"):
        assert added not in child


# ---- gaps a mutation run found (QA Plan B, MU-20): each test names what it pins ----
def test_main_wires_the_wait_to_the_engine_the_stop_file_the_limit_and_its_own_start(
        tmp_runtime, monkeypatch):
    s = _session(runtime_sec=77)
    seen, engine = {}, FakeEngine()

    def popen(argv, **kw):
        seen.update(kw)
        return engine

    def wait(*arguments):
        rec = session.load(s.id)
        seen.update(wait=arguments, during=(rec.status, rec.engine_pid))
        return None, 0
    monkeypatch.setattr(win_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(win_runner, "wait_for_end", wait)
    # a start time only for THIS process: asking about any other pid answers None
    monkeypatch.setattr(win_runner.procs, "pid_start", {os.getpid(): 111}.get)
    monkeypatch.setattr(win_runner.time, "monotonic", lambda: 500.0)
    assert win_runner.main(s.id) == 0
    run = session.run_dir(s)
    assert seen["wait"] == (engine, run / session.STOP_MARKER, 77, 500.0)
    assert seen["during"] == ("running", 4242)        # recorded before the wait begins
    assert str(seen["stdout"].name) == str(run / "engine.log")
    rec = session.load(s.id)
    assert (rec.runner_pid, rec.runner_start) == (os.getpid(), 111)


def test_an_exit_recorded_after_a_stop_keeps_the_stop(tmp_runtime):
    # `fsoctl stop` has already marked the session; the runner's own record comes later and
    # must not turn "stopped" into "exited" or replace who ended it
    s = _session()

    def stopped(rec):
        rec.status, rec.ended_by = "stopped", "stop"
    session.update(s.id, stopped)
    win_runner._record_exit(s.id, 1, None)
    rec = session.load(s.id)
    assert (rec.status, rec.ended_by) == ("stopped", "stop")
    assert set(rec.exit_info) == {"returncode", "signal", "at"} and rec.exit_info["at"] > 0


def test_a_failed_start_is_recorded_with_its_error_and_its_time(tmp_runtime):
    s = _session()
    assert win_runner._fail(s.id, "engine: no such file") == 1
    rec = session.load(s.id)
    assert rec.status == "failed"
    assert set(rec.exit_info) == {"error", "at"}
    assert rec.exit_info["error"] == "engine: no such file"


def test_a_close_request_that_cannot_be_sent_is_reported_and_the_engine_still_ended(
        monkeypatch, capsys):
    engine = FakeEngine(closes=False)

    def ask(pid):
        raise OSError("cannot attach to desktop")
    monkeypatch.setattr(win_runner, "_ask_close", ask)
    assert win_runner._end_engine(engine) == 1
    assert engine.killed and engine.waits == [win_runner.ENGINE_KILL_WAIT_S]
    err = capsys.readouterr().err
    assert "could not ask the engine to close: cannot attach to desktop" in err
    assert "no window was asked to close" in err


def test_the_close_request_goes_to_the_engines_own_windows(monkeypatch):
    asked = []
    monkeypatch.setattr(win_runner.win_desktop, "ask_close", lambda pid: asked.append(pid) or 2)
    assert win_runner._ask_close(4242) == 2 and asked == [4242]


def test_the_prefs_path_ends_in_exactly_one_separator_whatever_it_was_given():
    for given in ("C:\\run\\prefs", "C:\\run\\prefs\\", "C:\\run\\prefs/", "C:\\run\\prefs\\/"):
        child = envsetup.child_env_windows({}, given)
        assert child["FSO_PREFERENCES_PATH"] == "C:\\run\\prefs\\"


# ---- QA Plan B, CR-1 / ST-2: the runner is the only thing that ends a Windows engine ----
def _main_with(monkeypatch, engine, wait):
    """Prepare win_runner.main with a stand-in engine and a replaced wait; return the list
    that every close request to a window is noted in."""
    asked = []
    monkeypatch.setattr(win_runner.subprocess, "Popen", lambda argv, **kw: engine)
    # start times by process: the runner's own, and the stand-in engine's (pid 4242)
    monkeypatch.setattr(win_runner.procs, "pid_start", {os.getpid(): 111, 4242: 222}.get)
    monkeypatch.setattr(win_runner, "_ask_close", closer(engine, asked))
    monkeypatch.setattr(win_runner, "wait_for_end", wait)
    return asked


@pytest.mark.parametrize("error", [OSError("disk full"), RuntimeError("a bug"),
                                   KeyboardInterrupt()])
def test_a_runner_that_fails_after_starting_the_engine_ends_it_first(tmp_runtime, monkeypatch,
                                                                     capsys, error):
    # The handler for "the engine could not be started" used to catch this too: the session
    # was marked failed and the runner left, with the engine still running and nothing on
    # Windows to end it (a job has no time limit).
    s = _session()
    engine = FakeEngine(closes=False)

    def wait(*arguments):
        raise error
    asked = _main_with(monkeypatch, engine, wait)
    with pytest.raises(type(error)):
        win_runner.main(s.id)
    # ended at once, by force: this is not an orderly stop, and no window is asked anything
    assert engine.killed and asked == [] and engine.waits == [win_runner.ENGINE_KILL_WAIT_S]
    rec = session.load(s.id)
    assert (rec.status, rec.ended_by) == ("exited", "runner_error")
    assert rec.exit_info["returncode"] == 1 and rec.engine_pid == 4242
    assert rec.engine_start == 222        # the ENGINE's start time, recorded with its pid
    assert rec.runner_start == 111
    assert "ending the engine: the runner failed after starting it" in capsys.readouterr().err


def test_an_engine_that_outlives_the_forced_end_is_recorded_without_an_exit_code(
        tmp_runtime, monkeypatch, capsys):
    # mutation run, round 2: the code recorded when the forced end did not take was not pinned
    s = _session()
    engine = FakeEngine(closes=False, dies=False)

    def wait(*arguments):
        raise RuntimeError("a bug")
    _main_with(monkeypatch, engine, wait)
    with pytest.raises(RuntimeError):
        win_runner.main(s.id)
    rec = session.load(s.id)
    assert rec.ended_by == "runner_error" and rec.exit_info is None   # no code was known
    assert "could not end the engine" in capsys.readouterr().err


def test_a_log_that_cannot_be_written_does_not_stop_the_engine_being_ended(tmp_runtime,
                                                                          monkeypatch):
    # cross-family review XFA-2: the failure handler's first act was a line to stderr, which
    # is runner.log. On a full disk that write raises too, and the engine was left running.
    s = _session()
    engine = FakeEngine(closes=False)

    def wait(*arguments):
        raise OSError(28, "No space left on device")
    _main_with(monkeypatch, engine, wait)

    class Full:
        def write(self, text):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass
    monkeypatch.setattr(win_runner.sys, "stderr", Full())
    with pytest.raises(OSError):
        win_runner.main(s.id)
    assert engine.killed
    assert session.load(s.id).ended_by == "runner_error"


def test_a_runner_that_cannot_even_record_the_start_still_ends_the_engine(tmp_runtime,
                                                                         monkeypatch, capsys):
    s = _session()
    engine = FakeEngine()
    _main_with(monkeypatch, engine, lambda *a: (None, 0))
    real, calls = session.update, []

    def update(sid, change):
        calls.append(sid)
        if len(calls) >= 2:                         # the first write (the runner's pid) works
            raise PermissionError(13, "Permission denied")
        return real(sid, change)
    monkeypatch.setattr(win_runner.session, "update", update)
    with pytest.raises(PermissionError):
        win_runner.main(s.id)
    assert engine.killed
    err = capsys.readouterr().err
    assert "ending the engine" in err and "could not record how the run ended" in err


def test_an_engine_that_cannot_be_started_is_still_a_failed_session_and_nothing_to_end(
        tmp_runtime, monkeypatch, capsys):
    s = _session()

    def popen(argv, **kw):
        raise FileNotFoundError(2, "The system cannot find the file specified")
    monkeypatch.setattr(win_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(win_runner.procs, "pid_start", lambda pid: 111)
    assert win_runner.main(s.id) == 1
    assert session.load(s.id).status == "failed" and capsys.readouterr().err == ""


def test_an_engine_that_already_exited_is_not_ended_again_and_no_window_is_asked(
        tmp_runtime, monkeypatch):
    # Its pid may belong to another program by now, and in a visible session the runner is
    # on the user's own desktop: a close request there could reach the user's program.
    s = _session()
    engine = FakeEngine()
    engine.done = 0

    def wait(*arguments):
        raise OSError("the run folder went away")
    asked = _main_with(monkeypatch, engine, wait)
    with pytest.raises(OSError, match="went away"):
        win_runner.main(s.id)
    assert not engine.killed and asked == []
    rec = session.load(s.id)
    assert (rec.ended_by, rec.exit_info["returncode"]) == ("runner_error", 0)


def test_an_end_that_fails_does_not_hide_the_error_that_led_to_it(tmp_runtime, monkeypatch,
                                                                 capsys):
    s = _session()
    engine = FakeEngine()

    def refuse():
        raise PermissionError(5, "Access is denied")
    engine.kill = refuse

    def wait(*arguments):
        raise RuntimeError("the first error")
    _main_with(monkeypatch, engine, wait)
    with pytest.raises(RuntimeError, match="the first error"):
        win_runner.main(s.id)
    assert "could not end the engine" in capsys.readouterr().err
    assert session.load(s.id).ended_by == "runner_error"


def test_a_stop_that_arrived_before_the_engine_was_started_starts_none(tmp_runtime,
                                                                      monkeypatch):
    # e.g. a launch cancelled in its first moments: starting an engine only to end it took
    # 20 s and a forced end, because an engine with no window yet cannot be asked to close
    s = _session()
    (session.run_dir(s) / session.STOP_MARKER).touch()

    def never(argv, **kw):
        raise AssertionError("started an engine for a session already told to stop")
    monkeypatch.setattr(win_runner.subprocess, "Popen", never)
    monkeypatch.setattr(win_runner.procs, "pid_start", lambda pid: 111)
    assert win_runner.main(s.id) == win_runner.ENDED_BY_HARNESS
    rec = session.load(s.id)
    assert (rec.status, rec.ended_by, rec.engine_pid, rec.exit_info) == (
        "stopped", "stop", None, None)


def test_the_forced_end_happens_even_when_its_log_line_cannot_be_written(monkeypatch):
    # round-2 CRF-1: the line "no window was asked to close" is written before the kill; on
    # a full disk that write raised and the engine was not ended
    class Full:
        def write(self, text):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass
    engine = FakeEngine()
    monkeypatch.setattr(win_runner, "_ask_close", lambda pid: 0)
    monkeypatch.setattr(win_runner.sys, "stderr", Full())
    assert win_runner._end_engine(engine) == 1 and engine.killed


def test_an_engine_with_no_window_to_ask_is_ended_at_once(monkeypatch, capsys):
    # QA Plan B, CR-7 / ST-10: a stop that arrives before the engine has a window asked
    # nobody, then waited the whole 20 s for an answer
    engine = FakeEngine()
    monkeypatch.setattr(win_runner, "_ask_close", lambda pid: 0)
    assert win_runner._end_engine(engine) == 1
    assert engine.killed and engine.waits == [win_runner.ENGINE_KILL_WAIT_S]
    assert "no window was asked to close" in capsys.readouterr().err
