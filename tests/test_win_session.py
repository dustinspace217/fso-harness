"""Launching, stopping and cleaning up a session as the launcher does it on Windows
(cross-platform Plan B, Task 5), with the OS name and the process group replaced by fakes.

What is checked here is the launcher's own wiring: which runner module it starts, how it names
the session's desktop, what --visible changes, and that a session left half-started is ended
by the next cleanup (plan Review Focus 1)."""
import json
import os
import subprocess
import sys
import time

import pytest

from fsoctl import channel, launcher, platform, session
from fsoctl.errors import EXIT_ENGINE, EXIT_USAGE, HarnessError
from fsoctl.launcher import LaunchRequest


@pytest.fixture
def windows(monkeypatch):
    """The platform package's answers as they are on Windows."""
    monkeypatch.setattr(platform, "NAME", "windows")
    monkeypatch.setattr(platform, "RUNNER_MODULE", "fsoctl.platform.win_runner")
    monkeypatch.setattr(platform, "GROUP_TIME_LIMIT", False)
    monkeypatch.setattr(platform, "BUSY_FILES_REFUSE", True)
    monkeypatch.setattr(platform, "GROUPS_PER_LOGIN", True)


@pytest.fixture
def groups(monkeypatch, tmp_path):
    """A fake process-group module state: started groups are active until stopped."""
    class Fake:
        def __init__(self):
            self.active, self.stopped, self.started = set(), [], []
    fake = Fake()

    def start(unit, argv, env, log_path, props):
        fake.started.append({"unit": unit, "argv": argv, "props": props, "log": log_path})
        fake.active.add(unit)
    monkeypatch.setattr(launcher.scope, "start", start)
    monkeypatch.setattr(launcher.scope, "is_active", lambda unit: unit in fake.active)
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: unit not in fake.active)
    monkeypatch.setattr(launcher.scope, "active_units", lambda: [])

    def stop(unit, timeout=20):
        fake.stopped.append(unit)
        fake.active.discard(unit)
    monkeypatch.setattr(launcher.scope, "stop", stop)
    game = tmp_path / "FS2"
    game.mkdir()
    monkeypatch.setenv("FS2PATH", str(game))
    return fake


def test_launch_on_windows_names_the_desktop_and_the_win_runner(tmp_runtime, windows, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    started = groups.started[0]
    assert started["argv"] == [sys.executable, "-P", "-m", "fsoctl.platform.win_runner",
                               sess.id]
    assert started["unit"] == sess.scope == f"fso-harness-{sess.id}"
    assert f"Desktop=fso-harness-{sess.id}" in started["props"]
    assert any(p.startswith("MemoryMax=") for p in started["props"])
    rec = session.load(sess.id)
    assert rec.desktop == f"fso-harness-{sess.id}" and rec.visible is False


def test_a_visible_session_gets_no_desktop_of_its_own(tmp_runtime, windows, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable, visible=True))
    assert not any(p.startswith("Desktop=") for p in groups.started[0]["props"])
    rec = session.load(sess.id)
    assert rec.desktop is None and rec.visible is True


@pytest.mark.linux
def test_linux_starts_its_own_runner_with_no_desktop_property(tmp_runtime, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    started = groups.started[0]
    assert started["argv"] == [sys.executable, "-P", "-m", "fsoctl.runner", sess.id]
    assert not any(p.startswith("Desktop=") for p in started["props"])
    assert session.load(sess.id).desktop is None


@pytest.mark.linux
def test_visible_is_a_windows_option(tmp_runtime, groups):
    with pytest.raises(HarnessError) as e:
        launcher._validate(LaunchRequest(binary=sys.executable, visible=True))
    assert e.value.code == EXIT_USAGE and e.value.message == "--visible is a Windows option"
    assert groups.started == []


def test_cleanup_ends_a_job_whose_run_folder_says_starting_and_is_past_the_grace(
        tmp_runtime, windows, groups, capsys):
    # plan Review Focus 1: a launch killed after its process group existed. A Windows job has
    # no time limit of its own, so nothing else would ever end it.
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    assert session.load(sess.id).status == "starting"
    young = launcher.cleanup(0)
    assert young["removed"] == [] and groups.stopped == []          # inside the grace: kept

    def age(rec):
        rec.created -= launcher._STARTING_GRACE + 1
    session.update(sess.id, age)
    out = launcher.cleanup(10 ** 9)                 # ends it; far too young to be removed
    assert groups.stopped == [sess.scope] and out["removed"] == []
    assert f"session {sess.id}: never finished starting" in capsys.readouterr().err
    # the record says who ended it and why, and so does the audit log
    rec = session.load(sess.id)
    assert (rec.status, rec.ended_by) == ("failed", "abandoned_start")
    assert "abandoned_start" in (session.run_dir(rec) / "audit.log").read_text(encoding="utf-8")
    out = launcher.cleanup(0)
    assert out["removed"] == [sess.id] and not (tmp_runtime / sess.id).exists()


def _abandoned_start():
    """A session whose launch never finished and whose grace is over: the next cleanup ends
    its process group and, being old enough, removes its folder in the same run."""
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))

    def age(rec):
        rec.created -= launcher._STARTING_GRACE + 1
    session.update(sess.id, age)
    return sess


def _removal_refused(monkeypatch, times):
    """The folder remover as it is while a file in the folder is in use: its first `times`
    calls fail the way Windows fails them, later ones do the real work. Returns the calls."""
    real, calls = launcher.safefs.remove_tree, []

    def remove(parent, name):
        calls.append(name)
        if len(calls) <= times:
            raise PermissionError(13, "The process cannot access the file because it is "
                                      "being used by another process")
        real(parent, name)
    monkeypatch.setattr(launcher.safefs, "remove_tree", remove)
    return calls


def test_cleanup_waits_out_a_file_that_a_just_ended_process_still_has_open(
        tmp_runtime, windows, groups, monkeypatch):
    # Measured on CI's Windows machine (run 44083251849, 2026-11-17): the cleanup that had
    # just ended a session's processes could not delete its runner.log (Windows error 32,
    # in use). The job was empty, the engine's process not yet ended; a cleanup 16 ms later
    # removed the folder. The answer listed a failure that was over before anyone read it.
    sess = _abandoned_start()
    calls = _removal_refused(monkeypatch, 2)
    out = launcher.cleanup(0)
    assert out["removed"] == [sess.id] and out["failed"] == []
    assert calls == [sess.id] * 3 and not (tmp_runtime / sess.id).exists()


def test_a_folder_whose_file_stays_in_use_is_reported_after_a_bounded_wait(
        tmp_runtime, windows, groups, monkeypatch):
    sess = _abandoned_start()
    monkeypatch.setattr(session, "_BUSY_TRIES", 4)
    calls = _removal_refused(monkeypatch, 10 ** 6)
    out = launcher.cleanup(0)
    assert out["removed"] == [] and len(calls) == 4
    assert [f["path"] for f in out["failed"]] == [str(tmp_runtime / sess.id)]
    assert "being used" in out["failed"][0]["error"]
    # Nothing was removed here: the stand-in refuses before the real remover starts. (The
    # real one may have deleted files by then, the record among them: round-2 STF-7.)
    assert (tmp_runtime / sess.id / "session.json").exists()


def test_where_an_open_file_does_not_refuse_a_removal_is_tried_once(
        tmp_runtime, windows, groups, monkeypatch):
    # On Linux a file in use can be deleted; a refusal there is a real fault, said at once.
    sess = _abandoned_start()
    monkeypatch.setattr(platform, "BUSY_FILES_REFUSE", False)
    calls = _removal_refused(monkeypatch, 1)
    out = launcher.cleanup(0)
    assert out["removed"] == [] and calls == [sess.id] and len(out["failed"]) == 1


def test_a_running_session_is_never_ended_by_cleanup(tmp_runtime, windows, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))

    def run(rec):
        rec.status = "running"
        rec.created -= launcher._STARTING_GRACE + 1
    session.update(sess.id, run)
    assert launcher.cleanup(0)["removed"] == [] and groups.stopped == []


@pytest.mark.linux
def test_linux_leaves_a_stuck_start_to_the_scopes_own_time_limit(tmp_runtime, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))

    def age(rec):
        rec.created -= launcher._STARTING_GRACE + 1
    session.update(sess.id, age)
    assert launcher.cleanup(0)["removed"] == [] and groups.stopped == []


def test_root_check_is_skipped_on_windows(tmp_runtime, monkeypatch):
    # os.getuid does not exist on Windows; the multiplayer checks must not need it
    monkeypatch.delattr(os, "getuid", raising=False)
    with pytest.raises(HarnessError) as e:
        launcher._check_role(LaunchRequest(binary=sys.executable, match="m0a1b2c3d", role="host",
                                           pilot_mode="chooser"))
    assert "needs a created multiplayer pilot" in e.value.message


def test_paths_with_spaces_and_accents_survive_the_runner_command_line(tmp_runtime, windows,
                                                                       groups, monkeypatch):
    # plan Review Focus 3. The runner is named by module and session id only: no path of the
    # user's is on its command line at all; the engine's own argv carries them as list items.
    monkeypatch.setenv("FS2PATH", os.environ["FS2PATH"])
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable, mods=["My Mod", "Müller"]))
    argv = groups.started[0]["argv"]
    assert argv[1:] == ["-P", "-m", "fsoctl.platform.win_runner", sess.id]
    engine = launcher.build_argv(session.load(sess.id))
    assert "fso_harness_bridge,My Mod,Müller" in engine
    line = subprocess.list2cmdline(engine)
    assert '"fso_harness_bridge,My Mod,Müller"' in line


def test_cleanup_does_not_look_for_sound_sinks_where_sound_sessions_do_not_exist(
        tmp_runtime, windows, groups, monkeypatch):
    # Found by the first real launch on Windows (2026-11-16): every launch runs a cleanup,
    # the cleanup asked the Linux sound server for leftover sinks, and that code needs a
    # user id Windows does not have. No sound session can exist there (--sound is refused).
    def never(live):
        raise AssertionError("asked the sound server on Windows")
    monkeypatch.setattr(launcher.sound, "orphan_sinks", never)
    assert launcher._cleanup_sinks([], []) == {"removed_sinks": [], "kept_sinks": []}
    assert launcher.cleanup(0)["removed_sinks"] == []


def _flaky_read(monkeypatch, failures, error):
    """Make the channel's file read fail `failures` times with `error`, then answer; return
    the list its calls are counted in."""
    calls = []
    stat = os.stat(__file__)

    def read(base, rel):
        calls.append(rel)
        if len(calls) <= failures:
            raise error
        return b'{"state": "GS_STATE_GAME_PLAY", "frame": 7}', stat
    monkeypatch.setattr(channel.safefs, "read", read)
    monkeypatch.setattr(channel.time, "sleep", lambda s: None)
    return calls


@pytest.mark.parametrize("error", [
    FileNotFoundError(2, "gone"), PermissionError(13, "busy"),
    channel.safefs.Unsafe("ready.json changed while it was being opened"),
])
def test_on_windows_a_heartbeat_caught_mid_replace_is_read_again(tmp_runtime, windows, groups,
                                                                monkeypatch, error):
    # The bridge replaces ready.json there by delete-then-rename (rename cannot replace on
    # Windows), so for a moment per frame the file is missing or busy.
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    calls = _flaky_read(monkeypatch, 2, error)
    hb = channel.Channel(session.load(sess.id)).heartbeat()
    assert hb["frame"] == 7 and len(calls) == 3
    calls = _flaky_read(monkeypatch, channel._REPLACE_TRIES, error)      # really not there
    assert channel.Channel(session.load(sess.id)).heartbeat() is None
    assert len(calls) == channel._REPLACE_TRIES


@pytest.mark.linux
def test_on_linux_a_missing_heartbeat_is_reported_at_once(tmp_runtime, groups, monkeypatch):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    calls = _flaky_read(monkeypatch, 1, FileNotFoundError(2, "gone"))
    assert channel.Channel(session.load(sess.id)).heartbeat() is None and len(calls) == 1


def test_a_windows_session_is_told_not_to_pause_when_its_window_is_not_in_front(
        tmp_runtime, windows, groups):
    # Seen on the first Windows session (2026-11-16): the mission paused itself one second in.
    # A window on a hidden desktop is never the foreground one, and current engines read the
    # "pause if unfocused" setting from the mod's settings file, not from -no_unfocused_pause.
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    settings = session.prefs_dir(sess) / "data" / "fso_harness_bridge_settings.ini"
    assert settings.read_text() == "[Game]\nUnfocusedPause=false\n"
    assert "-no_unfocused_pause" in launcher.build_argv(session.load(sess.id))   # older engines


@pytest.mark.linux
def test_linux_sessions_get_no_settings_file_written_for_them(tmp_runtime, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    assert not (session.prefs_dir(sess) / "data").exists()


# ---- gaps a mutation run found (QA Plan B, MU-7, MU-8): each test names what it pins ----
def test_a_fresh_start_listed_first_does_not_hide_an_abandoned_one(tmp_runtime, windows, groups,
                                                                   monkeypatch, capsys):
    fresh, _ = launcher._start(LaunchRequest(binary=sys.executable))
    old, _ = launcher._start(LaunchRequest(binary=sys.executable))

    def age(rec):
        rec.created -= launcher._STARTING_GRACE + 1
    session.update(old.id, age)
    listed = [session.load(fresh.id), session.load(old.id)]
    monkeypatch.setattr(launcher.session, "list_sessions", lambda: listed)
    launcher._end_unattended(time.time())
    assert groups.stopped == [old.scope]
    assert f"session {old.id}: never finished starting" in capsys.readouterr().err


def test_the_renderer_is_read_from_the_engines_own_process(tmp_runtime, windows, groups,
                                                           monkeypatch):
    def ready(self, timeout, alive):
        def running(rec):
            rec.engine_pid, rec.status = 4321, "running"
        session.update(self.sess.id, running)
        return {"state": "GS_STATE_MAIN_MENU", "errors": 0, "last_error": None, "switches": {}}
    asked = []
    monkeypatch.setattr(channel.Channel, "wait_ready", ready)
    monkeypatch.setattr(launcher.procs, "loaded_libraries", lambda pid: asked.append(pid) or "")
    launcher.launch(LaunchRequest(binary=sys.executable))
    assert asked == [4321]


def test_the_heartbeat_is_read_again_after_the_set_gap(tmp_runtime, windows, groups, monkeypatch):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    _flaky_read(monkeypatch, 2, FileNotFoundError(2, "gone"))
    gaps = []
    monkeypatch.setattr(channel.time, "sleep", gaps.append)
    assert channel.Channel(session.load(sess.id)).heartbeat()["frame"] == 7
    assert gaps == [channel._REPLACE_GAP_SEC] * 2


# ---- QA Plan B, CR-1 / ST-2 / ST-3: a runner that is gone leaves nobody to end the engine ----
def _running_with_runner(groups, monkeypatch, ended=False, start_now=111, engine_ended=None,
                         **fields):
    """A session whose record says "running" under runner pid 10 (start time 111) with
    engine pid 20 (start time 222). The process facts answer, for the runner, `ended` and
    `start_now` as its start time today; for the engine, `engine_ended` (the runner's answer
    when not given) and its recorded start time. Any other pid is a wrong question."""
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))

    def mark(rec):
        rec.status, rec.runner_pid, rec.runner_start = "running", 10, 111
        rec.engine_pid, rec.engine_start = 20, 222
        for name, value in fields.items():
            setattr(rec, name, value)
    session.update(sess.id, mark)
    table = {10: (ended, start_now), 20: (ended if engine_ended is None else engine_ended, 222)}

    def fact(column, opposite=False):
        """One process fact, answered only for the recorded runner and engine: the
        questions must be put to THOSE processes."""
        def ask(pid):
            assert pid in table, f"asked about pid {pid!r}, not the runner (10) or engine (20)"
            value = table[pid][column]
            return (not value) if opposite else value
        return ask
    monkeypatch.setattr(launcher.procs, "pid_ended", fact(0))
    monkeypatch.setattr(launcher.procs, "pid_alive", fact(0, opposite=True))
    monkeypatch.setattr(launcher.procs, "pid_start", fact(1))
    return session.load(sess.id)


@pytest.mark.usefixtures("tmp_runtime", "windows")
@pytest.mark.parametrize("facts", [dict(ended=True), dict(ended=False, start_now=999)],
                         ids=["the runner ended", "its pid is another process now"])
def test_cleanup_ends_a_session_whose_runner_is_gone_and_whose_engine_still_runs(
        groups, monkeypatch, capsys, facts):
    # On Windows the runner is the only thing that ends an engine at its time limit or on a
    # stop request. With the runner gone the engine would run for good, holding a session
    # slot and a port.
    sess = _running_with_runner(groups, monkeypatch, **facts)
    launcher.cleanup(10 ** 9)
    assert groups.stopped == [sess.scope]
    rec = session.load(sess.id)
    assert (rec.status, rec.ended_by) == ("failed", "runner_lost")
    assert f"session {sess.id}: its runner is gone" in capsys.readouterr().err
    assert "runner_lost" in (session.run_dir(rec) / "audit.log").read_text(encoding="utf-8")


def test_a_runner_that_cannot_be_asked_about_is_not_taken_for_gone(tmp_runtime, windows, groups,
                                                                  monkeypatch):
    # e.g. a session started at another privilege level: every read fails. Not knowing is
    # never a reason to end a healthy session.
    sess = _running_with_runner(groups, monkeypatch, ended=False, start_now=None)
    launcher.cleanup(10 ** 9)
    assert groups.stopped == [] and session.load(sess.id).status == "running"


def test_a_session_whose_runner_is_alive_is_left_alone_whatever_its_age(tmp_runtime, windows,
                                                                       groups, monkeypatch):
    sess = _running_with_runner(groups, monkeypatch, ended=False, start_now=111,
                                created=1.0)
    launcher.cleanup(10 ** 9)
    assert groups.stopped == [] and session.load(sess.id).status == "running"


def test_leftovers_of_a_run_that_ended_cleanly_are_ended_and_its_record_is_kept(
        tmp_runtime, windows, groups, monkeypatch):
    # the engine exited with code 0, the runner recorded it and left; a child the engine
    # started is still in the group
    ended = {"returncode": 0, "signal": None, "at": 5.0}
    sess = _running_with_runner(groups, monkeypatch, ended=True, status="exited",
                                ended_by="exit", exit_info=ended)
    launcher.cleanup(10 ** 9)
    assert groups.stopped == [sess.scope]
    rec = session.load(sess.id)
    assert (rec.status, rec.ended_by, rec.exit_info) == ("exited", "exit", ended)
    assert "leftover" in (session.run_dir(rec) / "audit.log").read_text(encoding="utf-8")


def test_one_group_that_cannot_be_ended_does_not_stop_the_rest(tmp_runtime, windows, groups,
                                                              monkeypatch, capsys):
    # both made while their runners are alive (a launch runs the cleanup itself), then lost
    first = _running_with_runner(groups, monkeypatch, ended=False)
    second = _running_with_runner(groups, monkeypatch, ended=False)
    monkeypatch.setattr(launcher.procs, "pid_ended", lambda pid: True)
    stuck = session.list_sessions()[0]              # the one the cleanup reaches first
    other = second if stuck.id == first.id else first
    real_stop = launcher.scope.stop

    def stop(unit, timeout=20):
        if unit == stuck.scope:
            raise HarnessError(9, f"job {unit} still has 1 process(es) after it was ended")
        real_stop(unit, timeout)
    monkeypatch.setattr(launcher.scope, "stop", stop)
    out = launcher.cleanup(10 ** 9)
    assert groups.stopped == [other.scope]
    assert session.load(stuck.id).status == "running"       # not marked: it was not ended
    # in the answer too (cross-family review XFA-4): the MCP server passes on the JSON and
    # drops stderr, so a line there alone never reached an agent
    assert [f for f in out["failed"] if stuck.id in f["path"] and "still has 1" in f["error"]]
    assert not [f for f in out["failed"] if other.id in f["path"]]
    assert all(f["said"] is True for f in out["failed"])    # its own stderr line was written
    assert session.load(other.id).ended_by == "runner_lost"
    assert f"could not end session {stuck.id}" in capsys.readouterr().err


def test_the_cleanup_stop_does_not_wait_the_polite_time(tmp_runtime, windows, groups,
                                                       monkeypatch):
    # it runs under the launch lock at every launch
    sess = _running_with_runner(groups, monkeypatch, ended=True)
    waits = []
    monkeypatch.setattr(launcher.scope, "stop", lambda unit, timeout=20: waits.append(timeout))
    launcher.cleanup(10 ** 9)
    assert waits == [0] and sess.id


# ---- QA Plan B, ST-4 / TA-12 / SA: a session of another Windows login is not "gone" ----
def _seen_from_another_login(groups, monkeypatch, start_now=111):
    """A running session whose process group this login cannot find (a job's name is known
    only inside the Windows login that made it) while its runner, pid 10, still runs."""
    sess = _running_with_runner(groups, monkeypatch, ended=False, start_now=start_now)
    groups.active.clear()
    return sess


def test_cleanup_never_removes_the_folder_of_a_session_whose_runner_still_runs(
        tmp_runtime, windows, groups, monkeypatch):
    sess = _seen_from_another_login(groups, monkeypatch)
    out = launcher.cleanup(0)
    assert out["removed"] == [] and (tmp_runtime / sess.id / "session.json").exists()
    assert groups.stopped == []
    # the same record once pid 10 belongs to a process that started at another time
    monkeypatch.setattr(launcher.procs, "pid_start", lambda pid: 999)
    assert launcher.cleanup(0)["removed"] == [sess.id]


def test_a_session_of_another_login_still_counts_against_the_cap_and_keeps_its_port(
        tmp_runtime, windows, groups, monkeypatch):
    sess = _seen_from_another_login(groups, monkeypatch)
    assert launcher._gone(session.load(sess.id)) is False
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "1")
    with pytest.raises(HarnessError) as e:
        launcher.check_capacity()
    assert sess.id in e.value.message or "1" in e.value.message
    monkeypatch.setattr(launcher.procs, "pid_ended", lambda pid: True)   # both known ended
    assert launcher._gone(session.load(sess.id)) is True


def test_a_stop_from_a_login_that_cannot_see_the_group_says_what_it_did(
        tmp_runtime, windows, groups, monkeypatch):
    sess = _seen_from_another_login(groups, monkeypatch)
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: False)
    out = launcher.stop(sess.id)
    assert (session.run_dir(sess) / session.STOP_MARKER).exists()
    assert "cannot see" in out["note"] and "run folder" in out["note"]
    # round-2 CRF-3: the runner was asked and does the stopping. Nothing is "stopped" yet,
    # so the reply and the record do not say it is (the runner writes that when it has).
    assert out["runner_first"] == "asked" and out["status"] == "running"
    assert session.load(sess.id).status == "running"


def test_a_group_stop_that_fails_says_that_the_request_was_left(tmp_runtime, windows, groups,
                                                               monkeypatch):
    # round-2 CRF-4: the marker is written first; when the group then cannot be managed
    # from here the command fails, though a live runner is already acting on the request
    sess = _running_with_runner(groups, monkeypatch, ended=False)

    def stop(unit, timeout=20):
        raise HarnessError(EXIT_ENGINE, f"job {unit} may exist, but this login may not manage it")
    monkeypatch.setattr(launcher.scope, "stop", stop)
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: False)
    with pytest.raises(HarnessError) as e:
        launcher.stop(sess.id)
    assert "may not manage" in e.value.message and "request was left" in e.value.hint
    assert e.value.code == EXIT_ENGINE              # the failure's own code is kept


def test_where_groups_are_seen_from_every_login_the_record_is_not_asked(tmp_runtime, windows,
                                                                       groups, monkeypatch):
    # round-2 SAF-1 (its Linux half): a record that carries start times is believed only
    # where a group's name is hidden from other logins. Elsewhere the group's answer is whole.
    sess = _seen_from_another_login(groups, monkeypatch)
    assert launcher._gone(session.load(sess.id)) is False
    monkeypatch.setattr(platform, "GROUPS_PER_LOGIN", False)
    assert launcher._gone(session.load(sess.id)) is True
    # round-2 STFB-2: the same for the tools and for `stop`, which ask on lines of their own
    assert launcher._runs_out_of_sight(session.load(sess.id)) is False
    with pytest.raises(HarnessError) as e:
        launcher.require_running(sess.id)
    assert "is not running" in e.value.message
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: False)
    assert launcher.stop(sess.id)["status"] == "stopped"
    assert session.load(sess.id).status == "stopped"


@pytest.mark.linux
def test_on_linux_gone_is_what_the_scope_says(tmp_runtime, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    assert launcher._gone(session.load(sess.id)) is False
    groups.active.clear()
    assert launcher._gone(session.load(sess.id)) is True      # no start time is recorded here


# ---- QA Plan B, ST-8: on Windows "access denied" is also what a busy file says ----
def test_on_windows_a_refused_channel_file_is_busy_not_tampered(windows):
    # A scanner or an indexer that has a channel file open for a moment makes Windows
    # refuse it with the same error as a missing permission. "Tampered" tells an agent
    # not to retry and to treat the build as suspect, and the nightly review holds a PR on
    # it: too much for a file that was merely busy.
    err = channel._unusable(PermissionError(13, "Permission denied"))
    assert err.code == 7 and err.details.get("kind") != channel.TAMPERED
    assert "unusable" in err.message and "again" in err.hint
    # a link or another non-file in the channel is still tampering, on Windows too
    planted = channel._unusable(channel.safefs.Unsafe("cache is not a plain folder"))
    assert planted.details == {"kind": channel.TAMPERED}
    # and only a REFUSED file is "busy, try again": a full disk keeps the plain hint
    full = channel._unusable(OSError(28, "No space left on device"))
    assert "unusable" in full.message and "again" not in full.hint


@pytest.mark.linux
def test_on_linux_a_refused_channel_file_is_still_tampering():
    # there the harness made every channel file itself and nothing else opens them:
    # only the engine can have taken the permission away
    err = channel._unusable(PermissionError(13, "Permission denied"))
    assert err.details == {"kind": channel.TAMPERED}


def test_an_ended_runner_that_can_still_be_looked_up_is_not_alive(tmp_runtime, windows, groups,
                                                                  monkeypatch):
    # Seen on real Windows (the VM, 2026-11-17): for a moment after a process ends it can
    # still be opened and still answers with its start time. A stop from the session's own
    # login then carried the note meant for a stop from another login.
    sess = _running_with_runner(groups, monkeypatch, ended=False, start_now=111)
    monkeypatch.setattr(launcher.procs, "pid_alive", lambda pid: False)   # ended, still there
    monkeypatch.setattr(launcher.procs, "pid_ended", lambda pid: True)    # (and known ended)
    rec = session.load(sess.id)
    assert launcher._runner_known_alive(rec) is False
    groups.active.clear()
    assert launcher._gone(rec) is True
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: False)
    assert "note" not in launcher.stop(sess.id)


def test_a_tool_call_from_a_login_that_cannot_see_the_session_says_so(
        tmp_runtime, windows, groups, monkeypatch):
    # From another Windows login a live session read as "is not running ... or relaunch"
    # (VM, 2026-11-17: `list` over ssh showed it not alive). A relaunch would start a second
    # engine beside the first.
    sess = _seen_from_another_login(groups, monkeypatch)
    with pytest.raises(HarnessError) as e:
        launcher.require_running(sess.id)
    assert e.value.code == EXIT_ENGINE
    assert "another Windows login" in e.value.message and "relaunch" not in e.value.hint
    assert "login that started" in e.value.hint


def test_a_session_whose_runner_is_gone_too_is_still_plainly_not_running(
        tmp_runtime, windows, groups, monkeypatch):
    sess = _seen_from_another_login(groups, monkeypatch)
    monkeypatch.setattr(launcher.procs, "pid_alive", lambda pid: False)
    with pytest.raises(HarnessError) as e:
        launcher.require_running(sess.id)
    assert "is not running" in e.value.message and "relaunch" in e.value.hint


# ---- cross-family review XFA-1: "not known alive" is not "gone" ----
def test_a_session_nobody_can_be_asked_about_is_not_gone(tmp_runtime, windows, groups,
                                                         monkeypatch):
    # From another login the group is not found. If the runner cannot be asked about either
    # (every lookup fails), nothing is KNOWN: the first rule read that as gone, and cleanup
    # deleted the record of a session that may be healthy.
    sess = _seen_from_another_login(groups, monkeypatch)
    monkeypatch.setattr(launcher.procs, "pid_alive", lambda pid: False)   # cannot be opened
    monkeypatch.setattr(launcher.procs, "pid_start", lambda pid: None)    # cannot be asked
    monkeypatch.setattr(launcher.procs, "pid_ended", lambda pid: False)   # not known ended
    assert launcher._gone(session.load(sess.id)) is False
    assert launcher.cleanup(0)["removed"] == []
    assert (tmp_runtime / sess.id / "session.json").exists()


def test_an_engine_that_outlived_its_runner_is_not_gone_from_another_login(
        tmp_runtime, windows, groups, monkeypatch):
    # The runner is known gone, the engine is known to run, and this login cannot see the
    # group, so nothing here can end the engine. The record must stay for the login that
    # can, and stop must not say "stopped".
    sess = _running_with_runner(groups, monkeypatch, ended=True, engine_ended=False)
    groups.active.clear()
    assert launcher._gone(session.load(sess.id)) is False
    assert launcher.cleanup(0)["removed"] == [] and groups.stopped == []
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: False)
    with pytest.raises(HarnessError) as e:
        launcher.stop(sess.id)
    assert e.value.code == EXIT_ENGINE and "pid 20" in e.value.message
    assert "login that started" in e.value.hint
    assert session.load(sess.id).status == "running"          # not marked stopped: it is not


def test_a_session_whose_runner_and_engine_are_both_known_gone_is_gone(
        tmp_runtime, windows, groups, monkeypatch):
    sess = _running_with_runner(groups, monkeypatch, ended=True)
    groups.active.clear()
    assert launcher._gone(session.load(sess.id)) is True
    # the pids belong to other programs now: also known gone
    monkeypatch.setattr(launcher.procs, "pid_ended", lambda pid: False)
    monkeypatch.setattr(launcher.procs, "pid_start", lambda pid: 999)
    assert launcher._gone(session.load(sess.id)) is True


def test_a_record_that_has_its_end_is_gone_unless_one_of_its_processes_still_runs(
        tmp_runtime, windows, groups, monkeypatch):
    # The runner recorded the end: its word that the engine is over. Lookups that fail must
    # not keep an ended session's folder for good (an old ended session was removed from an
    # ssh login in the VM); a process KNOWN to run still does.
    done = dict(status="exited", ended_by="exit")
    sess = _running_with_runner(groups, monkeypatch, ended=True, **done)
    groups.active.clear()
    monkeypatch.setattr(launcher.procs, "pid_ended", lambda pid: False)   # not known ended
    monkeypatch.setattr(launcher.procs, "pid_alive", lambda pid: False)
    monkeypatch.setattr(launcher.procs, "pid_start", lambda pid: None)
    assert launcher._gone(session.load(sess.id)) is True
    monkeypatch.setattr(launcher.procs, "pid_alive", lambda pid: pid == 20)   # the engine runs
    monkeypatch.setattr(launcher.procs, "pid_start", {20: 222}.get)
    assert launcher._gone(session.load(sess.id)) is False


# ---- cross-family review XFA-6: a report asked from another login ----
def test_a_crash_report_from_another_login_says_the_session_still_runs(
        tmp_runtime, windows, groups, monkeypatch):
    from fsoctl import crash  # noqa: PLC0415
    sess = _seen_from_another_login(groups, monkeypatch)
    out = crash.report(sess.id)
    assert out["status"] == "running" and "another Windows login" in out["hints"][0]
    assert not any("gone" in h or "ended from outside" in h for h in out["hints"])


# ---- round 2, state seat on the fourth wave ----
def test_in_its_own_login_a_session_whose_engine_just_ended_is_plainly_not_running(
        tmp_runtime, windows, groups, monkeypatch):
    # STF-1: "a recorded process is alive and the session is not" is also true HERE, for the
    # half second in which the runner outlives an engine that exited or crashed. The group
    # is visible then; the caller must get the ordinary answer, not "another Windows login".
    sess = _running_with_runner(groups, monkeypatch, ended=False, engine_ended=True)
    assert sess.scope in groups.active                      # this login sees the group
    assert launcher._runs_out_of_sight(session.load(sess.id)) is False
    with pytest.raises(HarnessError) as e:
        launcher.require_running(sess.id)
    assert "is not running" in e.value.message and "crash_report" in e.value.hint
    groups.active.clear()                                   # the same facts, group unseen
    assert launcher._runs_out_of_sight(session.load(sess.id)) is True


def test_a_stop_in_the_sessions_own_login_trusts_its_own_group_stop(tmp_runtime, windows,
                                                                    groups, monkeypatch):
    # STF-2: the group was seen and has just been ended by force; a process that is still
    # in its last moment must not turn the reply into "asked" or a refusal.
    sess = _running_with_runner(groups, monkeypatch, ended=False)    # both still "alive"
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: False)
    out = launcher.stop(sess.id)
    assert groups.stopped == [sess.scope]
    assert out["status"] == "stopped" and "note" not in out
    assert session.load(sess.id).ended_by == "stop"


def test_a_stop_from_another_login_that_could_not_leave_its_request_refuses(
        tmp_runtime, windows, groups, monkeypatch):
    # STF-3: nothing was left and nothing was stopped; the reply said the request was left
    import pathlib  # noqa: PLC0415
    sess = _seen_from_another_login(groups, monkeypatch)
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: False)
    real = pathlib.Path.touch

    def touch(self, *arguments, **keywords):
        if self.name == session.STOP_MARKER:
            raise PermissionError(13, "Permission denied")
        return real(self, *arguments, **keywords)
    monkeypatch.setattr(pathlib.Path, "touch", touch)
    with pytest.raises(HarnessError) as e:
        launcher.stop(sess.id)
    assert e.value.code == EXIT_ENGINE and "could not be left" in e.value.message
    assert "login that started" in e.value.hint
    assert session.load(sess.id).status == "running"


def test_a_cleanup_that_ends_a_session_someone_asked_to_stop_records_a_stop(
        tmp_runtime, windows, groups, monkeypatch):
    # STF-6: a stop in flight has left its marker; a cleanup that gets to the group first
    # must not label that end "failed / runner_lost"
    sess = _running_with_runner(groups, monkeypatch, ended=True)
    (session.run_dir(sess) / session.STOP_MARKER).touch()
    launcher.cleanup(10 ** 9)
    rec = session.load(sess.id)
    assert groups.stopped == [sess.scope]
    assert (rec.status, rec.ended_by) == ("stopped", "stop")


def test_a_runner_that_died_before_it_started_an_engine_is_gone(tmp_runtime, windows, groups,
                                                               monkeypatch):
    # the cell of the rule no test held: runner known gone, no engine recorded, group not found
    sess = _running_with_runner(groups, monkeypatch, ended=True, engine_pid=None,
                                engine_start=None, status="starting", created=1.0)
    groups.active.clear()
    assert launcher._gone(session.load(sess.id)) is True
    assert launcher.cleanup(0)["removed"] == [sess.id]      # old enough: past the grace


def test_a_session_of_another_login_keeps_its_port(tmp_runtime, windows, groups, monkeypatch):
    # the cap was pinned for this case; the port choice was not
    sess = _seen_from_another_login(groups, monkeypatch)

    def give(rec):
        rec.port, rec.network = 7808, "real"
    session.update(sess.id, give)
    assert launcher.host_port(None) != 7808
    monkeypatch.setattr(launcher.procs, "pid_ended", lambda pid: True)    # and once it is over
    assert launcher.host_port(None) == 7808


@pytest.mark.linux
def test_on_linux_a_failed_group_stop_does_not_speak_of_a_request_left(tmp_runtime, groups,
                                                                       monkeypatch):
    # STF-5: the Linux runner acts on a signal; the marker only labels the cause
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))

    def stop(unit, timeout=20):
        raise HarnessError(EXIT_ENGINE, f"scope {unit} did not stop", "look at the journal")
    monkeypatch.setattr(launcher.scope, "stop", stop)
    with pytest.raises(HarnessError) as e:
        launcher.stop(sess.id)
    assert e.value.hint == "look at the journal"


# ---- round 2, the final mutation run (MUF-12): the fourth wave's unpinned lines ----
def test_an_engine_alone_is_enough_to_say_the_session_runs_elsewhere(tmp_runtime, windows,
                                                                     groups, monkeypatch):
    # the runner is gone, the engine runs, the group cannot be seen: a tool call and a crash
    # report must still say "runs in another Windows login", not "not running, relaunch"
    from fsoctl import crash  # noqa: PLC0415
    sess = _running_with_runner(groups, monkeypatch, ended=True, engine_ended=False)
    groups.active.clear()
    with pytest.raises(HarnessError) as e:
        launcher.require_running(sess.id)
    assert "another Windows login" in e.value.message
    assert "another Windows login" in crash.report(sess.id)["hints"][0]


def test_a_start_is_abandoned_from_the_moment_its_grace_is_over(tmp_runtime, windows, groups):
    sess, _ = launcher._start(LaunchRequest(binary=sys.executable))
    rec = session.load(sess.id)
    now = rec.created + launcher._STARTING_GRACE
    assert launcher._unattended(rec, now) == "abandoned_start"        # at the grace exactly
    assert launcher._unattended(rec, now - 0.001) is None


def test_the_automatic_cleanup_prints_each_failure_once(tmp_runtime, monkeypatch, capsys):
    # a session that could not be ended has had its line already ("said"); a folder that
    # could not be removed has not, and one skipped line must not swallow the next
    monkeypatch.setattr(launcher, "_cleanup_locked", lambda *ages: {
        "removed": [], "failed": [{"path": "A", "error": "could not end", "said": True},
                                  {"path": "B", "error": "in use"},
                                  {"path": "C", "error": "could not end", "said": True},
                                  {"path": "D", "error": "busy"}]})
    launcher._auto_cleanup()
    err = capsys.readouterr().err
    assert "could not remove B: in use" in err and "could not remove D: busy" in err
    assert "could not remove A" not in err and "could not remove C" not in err


def test_an_end_that_cannot_be_recorded_is_said_and_does_not_stop_the_cleanup(
        tmp_runtime, windows, groups, monkeypatch, capsys):
    sess = _running_with_runner(groups, monkeypatch, ended=True)
    real = session.update

    def update(sid, change):
        if sid == sess.id:
            raise PermissionError(13, "Permission denied")
        return real(sid, change)
    monkeypatch.setattr(launcher.session, "update", update)
    launcher.cleanup(10 ** 9)
    assert groups.stopped == [sess.scope]
    assert f"could not record the end of session {sess.id}" in capsys.readouterr().err


def test_the_cleanups_audit_line_is_filed_under_cleanup(tmp_runtime, windows, groups,
                                                        monkeypatch):
    sess = _running_with_runner(groups, monkeypatch, ended=True)
    launcher.cleanup(10 ** 9)
    lines = (session.run_dir(sess) / "audit.log").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["kind"] == "cleanup"
