"""The launcher's cleanup rules for a session nothing looks after, against real Job Objects,
desktops and processes (QA Plan B: CR-1, ST-3, ST-5, TA-2, TA-7).

On Windows the runner is the only thing that ends an engine at its time limit or on a stop
request. These tests start a stand-in runner (a Python process that starts a child, the
"engine", and sleeps) inside a real session's job and on its own desktop, then take the
runner away, or never let the session get past "starting", and run the real cleanup."""
import subprocess
import sys
import time

import pytest

from fsoctl import launcher, session
from fsoctl.platform import win_desktop, win_procs, win_scope

# The stand-in runner: starts the "engine" with its handles handed on (as the real runner
# does), writes both pids, sleeps.
RUNNER = r"""
import os, subprocess, sys, time
engine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                          close_fds=False)
open(sys.argv[1], "w").write(f"{os.getpid()} {engine.pid}")
time.sleep(120)
"""


def wait_for(condition, seconds=20.0):
    """Poll `condition` until true or the time is up; return its last value."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:            # bounded
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    return condition()


@pytest.fixture
def started(tmp_runtime, tmp_path, child_env):
    """A real session record whose job holds a stand-in runner and its "engine", on the
    session's own desktop. Yields (record, runner pid, engine pid); ends the job afterwards."""
    sess = session.new_session(kind="game", binary=sys.executable, fs2path=str(tmp_path),
                               mods=[], mission=None, renderer="opengl", width=1, height=1,
                               switches={}, allow_eval=False, extra_args=[], runtime_sec=60)
    pids = tmp_path / "pids.txt"
    props = [*win_scope.caps(runtime_sec=60, cpu_cores=1, tasks=16, mem_kb=1048576),
             f"Desktop={sess.scope}"]
    # The log goes where the product's goes, inside the run folder (round-2 TAF-14): "its
    # folder removed" must be shown for a folder a just-ended process had a file open in.
    win_scope.start(sess.scope, [sys.executable, "-c", RUNNER, str(pids)], child_env,
                    session.run_dir(sess) / "runner.log", props)
    try:
        assert wait_for(lambda: pids.exists() and len(pids.read_text().split()) == 2)
        runner, engine = (int(p) for p in pids.read_text().split())
        yield sess, runner, engine
    finally:
        win_scope.stop(sess.scope, timeout=10)


def _record_running(sess, runner, engine):
    """Write what the real runner records once its engine runs."""
    def mark(rec):
        rec.status, rec.engine_pid, rec.desktop = "running", engine, rec.scope
        rec.runner_pid, rec.runner_start = runner, win_procs.pid_start(runner)
    return session.update(sess.id, mark)


def test_an_engine_whose_runner_was_ended_is_still_found_and_the_cleanup_ends_it(started):
    sess, runner, engine = started
    rec = _record_running(sess, runner, engine)
    assert launcher._runner_known_gone(rec) is False
    assert launcher._unattended(rec, time.time()) is None
    # end the runner alone, by force, as a user or a crash would
    subprocess.run(["taskkill", "/PID", str(runner), "/F"], capture_output=True, timeout=30,
                   check=True)
    assert wait_for(lambda: win_procs.pid_ended(runner))
    assert launcher._runner_known_alive(rec) is False      # ended, even while still openable
    # the engine inherited the job's handle and the desktop's: both are still there by name
    assert win_scope.is_active(sess.scope) and win_procs.in_group(engine, sess.scope)
    assert win_desktop.on_desktop(sess.scope, lambda: "on it") == "on it"
    assert launcher._runner_known_gone(rec) is True
    assert launcher._unattended(rec, time.time()) == "runner_lost"
    out = launcher.cleanup(10 ** 9)                 # ends it; far too young to be removed
    assert out["removed"] == []
    assert wait_for(lambda: win_procs.pid_ended(engine)) and win_scope.is_gone(sess.scope)
    after = session.load(sess.id)
    assert (after.status, after.ended_by) == ("failed", "runner_lost")


def test_a_session_whose_runner_runs_is_left_alone_by_the_cleanup(started):
    sess, runner, engine = started
    before = _record_running(sess, runner, engine)
    assert launcher._runner_known_alive(before) is True
    launcher.cleanup(10 ** 9)
    assert win_procs.pid_alive(runner) and win_procs.pid_alive(engine)
    after = session.load(sess.id)
    assert (after.status, after.ended_by, after.runner_start) == (
        "running", None, before.runner_start)


def test_an_abandoned_start_is_ended_with_its_desktop_and_its_folder_removed(started,
                                                                            tmp_runtime):
    # plan Review Focus 1, for real: the launch was ended after it made the process group,
    # and the record never got past "starting"
    sess, runner, engine = started

    def age(rec):
        rec.created -= launcher._STARTING_GRACE + 1
    session.update(sess.id, age)
    out = launcher.cleanup(0)
    assert wait_for(lambda: win_procs.pid_ended(runner) and win_procs.pid_ended(engine))
    assert win_scope.is_gone(sess.scope)
    # Removed by the very cleanup that ended it, though the processes just ended had its
    # runner.log open. On CI's Windows machine the engine's process was still in its last
    # instant when the cleanup returned, and the file "in use" (run 44083251849: a cleanup
    # 16 ms later could remove it); the cleanup's short retry waits that out. The whole
    # answer goes in the message as text: a folder that could not be removed says why there.
    assert out["removed"] == [sess.id], f"{out}"
    assert not (tmp_runtime / sess.id).exists()
    with pytest.raises(OSError):                    # its desktop went with its last process
        wait_for(lambda: win_desktop.on_desktop(sess.scope, lambda: False), 5)
