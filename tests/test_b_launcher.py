"""Launcher lifecycle fixes from QA wave 1 (group B): capacity, cleanup, stop, launch failure paths,
input refusals, status staleness, alive() cost.

Nothing here starts a real scope or engine: scope.* and the bridge channel are stubbed. alive() is
NOT stubbed in the capacity tests (QA TA-2: stubbing it hid starting sessions).
"""
import fcntl
import os
import shutil
import time
from pathlib import Path

import pytest

from fsoctl import channel, cli, launcher, paths, scope, session
from fsoctl.errors import (EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE,
                           HarnessError)
from fsoctl.launcher import LaunchRequest


def _mk(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[])
    status = kw.pop("status", None)
    base.update(kw)
    s = session.new_session(**base)
    if status:
        s.status = status
        session.save(s)
    return s


def _raises(code, fn, *a, **kw) -> HarnessError:
    """Call fn, assert it raises HarnessError with `code` and a non-empty message and hint."""
    with pytest.raises(HarnessError) as e:
        fn(*a, **kw)
    assert e.value.code == code
    assert e.value.message and e.value.hint
    return e.value


@pytest.fixture
def fs2(tmp_path, monkeypatch):
    """A throwaway FS2PATH (the bridge gets installed into it)."""
    d = tmp_path / "FS2"
    d.mkdir()
    monkeypatch.setenv("FS2PATH", str(d))
    monkeypatch.setattr(scope, "active_units", lambda: [])   # no real harness scopes block it
    return d


@pytest.fixture
def scopes(monkeypatch):
    """Fake systemd: a dict unit -> state word; start() makes a unit active, stop() makes it
    inactive. Records stop() calls in .stopped."""
    class Fake:
        def __init__(self):
            self.states: dict[str, str] = {}
            self.stopped: list[str] = []
    fake = Fake()
    monkeypatch.setattr(scope, "state", lambda unit: fake.states.get(unit, "inactive"))
    monkeypatch.setattr(scope, "is_active", lambda unit: fake.states.get(unit) == "active")

    def stop(unit, timeout=20):
        fake.stopped.append(unit)
        fake.states[unit] = "inactive"
    monkeypatch.setattr(scope, "stop", stop)

    def start(unit, argv, env, log_path, props):
        fake.states[unit] = "active"
        return 4242
    monkeypatch.setattr(scope, "start", start)
    return fake


# ---- capacity (QA SL-1 / CR-6 / TA-1 / SA-7) --------------------------------------------------

def test_capacity_counts_a_starting_session(tmp_runtime, scopes, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "1")
    s = _mk()                       # status "starting", engine_pid None
    scopes.states[s.scope] = "active"
    e = _raises(EXIT_REFUSED, launcher.check_capacity)
    assert "1 sessions" in e.message


def test_capacity_under_the_cap_does_not_raise(tmp_runtime, scopes, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "2")
    s = _mk()
    scopes.states[s.scope] = "active"
    _mk(status="running")           # its scope is gone: must not count
    assert [x.id for x in launcher.check_capacity()] == [s.id]


def test_launch_lock_is_held_while_a_session_is_created(tmp_runtime, scopes, fs2, monkeypatch):
    # Another launch holds launch.lock: this one must wait for it (bounded), not race past the
    # capacity check (two parallel launches both passed a cap of 1 before the fix).
    monkeypatch.setattr(launcher, "_LOCK_WAIT", 0.3)
    with open(paths.runtime_root() / "launch.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        _raises(EXIT_TIMEOUT, launcher.launch, LaunchRequest(binary="/bin/true"))
    assert session.list_sessions() == []


# ---- cleanup (QA SL-2 / CR-4) -----------------------------------------------------------------

def test_cleanup_keeps_starting_and_still_stopping_sessions(tmp_runtime, scopes):
    starting = _mk()
    scopes.states[starting.scope] = "active"
    stopping = _mk(status="running")
    scopes.states[stopping.scope] = "deactivating"
    done = _mk(status="exited")
    r = launcher.cleanup(0)
    assert r["removed"] == [done.id]
    assert session.run_dir(starting).is_dir() and session.run_dir(stopping).is_dir()


def test_cleanup_keeps_a_starting_record_even_without_a_scope(tmp_runtime, scopes):
    # Between new_session and scope.start a launch's record has no scope yet.
    s = _mk()
    assert launcher.cleanup(0)["removed"] == []
    assert session.run_dir(s).is_dir()


def _old(path: Path, seconds: float = 7200) -> Path:
    """Backdate a folder's mtime (default two hours)."""
    t = time.time() - seconds
    os.utime(path, (t, t))
    return path


def test_cleanup_removes_old_unreferenced_side_folders(tmp_runtime, scopes, tmp_path):
    game = tmp_path / "game" / "data"
    game.mkdir(parents=True)
    (game / "keep.txt").write_text("game data")
    root = paths.runtime_root()
    stale_shadow = root / "shadows" / "aaaaaaaa"
    stale_shadow.mkdir(parents=True)
    (stale_shadow / "data").symlink_to(game)     # shadows link into FS2PATH
    _old(stale_shadow)
    used_shadow = _old(_mkdir(root / "shadows" / "bbbbbbbb"))
    young_shadow = _mkdir(root / "shadows" / "cccccccc")
    foreign = _old(_mkdir(root / "shadows" / "not-ours"))
    stale_fred = root / "fred-missions" / "dddddddd"
    stale_fred.mkdir(parents=True)
    (stale_fred / "m.fs2").write_text("x")
    _old(stale_fred)
    used_fred = root / "fred-missions" / "eeeeeeee"
    used_fred.mkdir(parents=True)
    _old(used_fred)
    live = _mk(status="running", cwd=str(used_shadow), kind="game")
    scopes.states[live.scope] = "active"
    fred = _mk(status="running", kind="qtfred", mission=str(used_fred / "m.fs2"))
    scopes.states[fred.scope] = "active"

    r = launcher.cleanup(600)
    assert sorted(r["removed_folders"]) == sorted([str(stale_shadow), str(stale_fred)])
    assert not stale_shadow.exists() and not stale_fred.exists()
    assert (game / "keep.txt").read_text() == "game data"   # the link was not followed
    for kept in (used_shadow, young_shadow, foreign, used_fred):
        assert kept.is_dir()


def _mkdir(p: Path) -> Path:
    """mkdir -p and return the path."""
    p.mkdir(parents=True)
    return p


# ---- stop (QA SL-2 / CR-4, TA-3, SL-7) --------------------------------------------------------

def test_stop_twice_keeps_an_exited_session_and_its_exit_info(tmp_runtime, scopes):
    s = _mk(status="exited")
    info = {"returncode": -11, "signal": "SIGSEGV", "at": 1.0}
    session.update(s.id, lambda x: setattr(x, "exit_info", info))
    for _ in range(2):
        r = launcher.stop(s.id)
        assert r["status"] == "exited" and r["exit_info"] == info
    assert session.load(s.id).status == "exited"


def test_stop_marks_the_stop_for_the_runner(tmp_runtime, scopes):
    s = _mk(status="running")
    scopes.states[s.scope] = "active"
    r = launcher.stop(s.id)
    assert r["status"] == "stopped"
    assert (session.run_dir(s) / session.STOP_MARKER).exists()
    assert session.load(s.id).ended_by == "stop"
    assert scopes.stopped == [s.scope]


def test_stop_without_a_session_folder_still_stops_its_scope(tmp_runtime, scopes):
    s = _mk(status="running")
    scopes.states[s.scope] = "active"
    shutil.rmtree(session.run_dir(s))       # e.g. a concurrent cleanup removed it
    r = launcher.stop(s.id)
    assert scopes.stopped == [s.scope] and r["id"] == s.id
    _raises(EXIT_NOT_FOUND, launcher.stop, s.id)   # scope gone too: nothing left to stop


# ---- launch failure paths (QA CR-5 / SL-9, TA-4, SF-1, CR-15) ---------------------------------

def test_scope_start_failure_stops_the_session_and_names_it(tmp_runtime, fs2, scopes,
                                                           monkeypatch):
    def boom(*a, **k):
        raise HarnessError(EXIT_LAUNCH, "scope did not become active", "is systemd running?")
    monkeypatch.setattr(scope, "start", boom)
    e = _raises(EXIT_LAUNCH, launcher.launch, LaunchRequest(binary="/bin/true"))
    sid = e.details["id"]
    assert session.load(sid).status == "stopped"


def test_readiness_failure_stops_the_session_and_quotes_bridge_errors(tmp_runtime, fs2, scopes,
                                                                     monkeypatch):
    def no_heartbeat(self, timeout, alive):
        raise HarnessError(EXIT_TIMEOUT, "no bridge heartbeat after 1s", "take a screenshot")
    monkeypatch.setattr(channel.Channel, "wait_ready", no_heartbeat)
    monkeypatch.setattr(channel.Channel, "heartbeat",
                        lambda self: {"state": "GS_STATE_INITIAL_PLAYER_SELECT", "errors": 2,
                                      "last_error": "boom in hook",
                                      "switches": {"skip_briefing": "failed: x"}})
    e = _raises(EXIT_TIMEOUT, launcher.launch, LaunchRequest(binary="/bin/true"))
    assert scopes.stopped == [session.load(e.details["id"]).scope]
    assert "boom in hook" in e.message
    assert e.details["heartbeat"]["switches"] == {"skip_briefing": "failed: x"}


def test_launch_reports_refresh_bridge_errors_and_unknown_renderer(tmp_runtime, fs2, scopes,
                                                                  monkeypatch):
    hb = {"state": "GS_STATE_MAIN_MENU", "errors": 1, "last_error": "x",
          "switches": {"skip_movies": "done"}}
    monkeypatch.setattr(channel.Channel, "wait_ready", lambda self, timeout, alive: hb)
    monkeypatch.setattr(launcher.renderer, "read_maps", lambda pid: "")
    r = launcher.launch(LaunchRequest(binary="/bin/true"))
    assert r["bridge_refreshed"] is True       # first install into an empty FS2PATH
    assert r["errors"] == 1 and r["last_error"] == "x"
    assert r["switches"] == {"skip_movies": "done"}
    assert r["renderer"] == "unknown"
    assert any(w.startswith("renderer unknown") for w in r["warnings"])
    sess = session.load(r["id"])
    assert sess.runtime_sec == 1800 and sess.memory_max.endswith("K")
    r2 = launcher.launch(LaunchRequest(binary="/bin/true"))
    assert r2["bridge_refreshed"] is False


def test_qtfred_readiness_timeout_is_a_timeout(tmp_runtime, monkeypatch):
    s = _mk(kind="qtfred")
    monkeypatch.setattr(launcher.xwin, "main_window", lambda *a: None)
    _raises(EXIT_TIMEOUT, launcher._wait_window, s.id, 0.3)


# ---- input refusals (QA CR-14, SA-4 / C9, SA-5 / C10, CR-9) -----------------------------------

def test_existing_pilot_needs_a_source(tmp_runtime, fs2):
    _raises(EXIT_USAGE, launcher.launch,
            LaunchRequest(binary="/bin/true", pilot_mode="existing", pilot_name="Alpha"))
    assert session.list_sessions() == []


def test_copy_pilot_copies_only_that_pilots_files(tmp_path):
    src, prefs = tmp_path / "players", tmp_path / "prefs"
    src.mkdir()
    for n in ("Alpha.json", "Alpha.Main.csg", "Alpha.txt", "Beta.json"):
        (src / n).write_text(n)
    launcher._copy_pilot(src, "Alpha", prefs)
    got = sorted(p.name for p in (prefs / "data" / "players").iterdir())
    assert got == ["Alpha.Main.csg", "Alpha.json"]
    e = _raises(EXIT_LAUNCH, launcher._copy_pilot, src, "Gamma", prefs)
    assert "Gamma" in e.message


@pytest.mark.parametrize("mission", ["SM1-01.fs2", "/elsewhere/m.fs2"])
def test_qtfred_refuses_a_mission_that_is_not_a_staged_copy(tmp_runtime, fs2, mission):
    e = _raises(EXIT_REFUSED, launcher.launch,
                LaunchRequest(binary="/bin/true", kind="qtfred", mission=mission))
    assert "fred_open" in e.hint
    assert session.list_sessions() == []


def test_qtfred_accepts_a_staged_copy(tmp_runtime, fs2):
    staged = paths.runtime_root() / "fred-missions" / "abcd1234" / "m.fs2"
    staged.parent.mkdir(parents=True)
    staged.write_text("x")
    launcher._validate(LaunchRequest(binary="/bin/true", kind="qtfred", mission=str(staged)))
    launcher._validate(LaunchRequest(binary="/bin/true", kind="qtfred"))     # untitled


@pytest.mark.parametrize("arg", ["-portable_mode", "-output_sexps", "-output_scripting",
                                 "-profile_write_file", "-mod", "-start_mission", "-pilot"])
def test_extra_args_that_escape_the_session_are_refused(tmp_runtime, fs2, arg):
    e = _raises(EXIT_USAGE, launcher._validate,
                LaunchRequest(binary="/bin/true", extra_args=["-seed", "3", arg]))
    assert arg in e.message


def test_output_flags_are_allowed_in_a_shadow_folder(tmp_runtime, fs2):
    shadow = paths.runtime_root() / "shadows" / "abcd1234"
    shadow.mkdir(parents=True)
    launcher._validate(LaunchRequest(binary="/bin/true", cwd=str(shadow),
                                     extra_args=["-output_sexps", "-profile_write_file"]))
    _raises(EXIT_USAGE, launcher._validate,
            LaunchRequest(binary="/bin/true", cwd=str(shadow), extra_args=["-portable_mode"]))


def test_engine_binary_must_be_an_executable_file(tmp_runtime, fs2, tmp_path):
    _raises(EXIT_USAGE, launcher._validate, LaunchRequest(binary=str(tmp_path / "nope")))
    plain = tmp_path / "plain"
    plain.write_text("not executable")
    _raises(EXIT_USAGE, launcher._validate, LaunchRequest(binary=str(plain)))


# ---- status staleness (QA SL-10 / SF-5) and alive() cost (QA CR-13) ---------------------------

@pytest.mark.parametrize("case", [({"state": "X", "age_sec": 7.5}, 7.5, True),
                                  ({"state": "X", "age_sec": 1.0}, 1.0, False),
                                  ({"state": "X"}, None, None),   # channel without age_sec
                                  (None, None, None)])
def test_status_reports_heartbeat_age(tmp_runtime, scopes, monkeypatch, case):
    hb, age, stale = case
    s = _mk(status="running")
    monkeypatch.setattr(channel.Channel, "heartbeat", lambda self: hb)
    r = launcher.status(s.id)
    assert r["heartbeat_age_sec"] == age and r["stale"] is stale


def test_alive_checks_the_pid_before_asking_systemd(tmp_runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(scope, "is_active", lambda unit: calls.append(unit) or True)
    dead = _mk(status="running", engine_pid=2 ** 22 + 12345)   # above pid_max: never exists
    assert launcher.alive(dead) is False and calls == []
    me = _mk(status="running", engine_pid=os.getpid())
    assert launcher.alive(me) and launcher.alive(me) and launcher.alive(me)
    assert calls == [me.scope]     # once per second per session, not once per call


@pytest.mark.parametrize("arg", ["-portable_mode", "-mod", "-start_mission", "-pilot"])
def test_harness_only_args_are_refused_even_in_a_shadow_folder(tmp_runtime, fs2, arg):
    shadow = paths.runtime_root() / "shadows" / "abcd1234"
    shadow.mkdir(parents=True)
    e = _raises(EXIT_USAGE, launcher._validate,
                LaunchRequest(binary="/bin/true", cwd=str(shadow), extra_args=[arg]))
    assert arg in e.message


@pytest.mark.parametrize("arg", ["-output_sexps", "-profile_write_file"])
def test_output_flags_are_refused_in_a_working_folder_outside_shadows(tmp_runtime, fs2, arg):
    # Inside the runtime root (any other cwd is refused on its own) but not a shadow folder.
    elsewhere = _mkdir(paths.runtime_root() / "work")
    e = _raises(EXIT_USAGE, launcher._validate,
                LaunchRequest(binary="/bin/true", cwd=str(elsewhere), extra_args=[arg]))
    assert arg in e.message


# ---- cancellation (fix-wave-2 R3: SL2-1 / CR2-1 / Y-2) ---------------------------------------
# SIGTERM inside fsoctl raises SystemExit(143) (contract C8); Ctrl-C raises KeyboardInterrupt.
# Neither is a HarnessError, so the launch's failure handlers never ran and the engine kept a
# session slot until RuntimeMaxSec.

def test_sigterm_during_readiness_stops_the_session(tmp_runtime, fs2, scopes, monkeypatch):
    def interrupted(self, timeout, alive):
        raise SystemExit(143)
    monkeypatch.setattr(channel.Channel, "wait_ready", interrupted)
    with pytest.raises(SystemExit):
        launcher.launch(LaunchRequest(binary="/bin/true"))
    (sess,) = session.list_sessions()
    assert scopes.stopped == [sess.scope] and session.load(sess.id).status == "stopped"


def test_interrupt_while_the_scope_starts_stops_the_session(tmp_runtime, fs2, scopes,
                                                            monkeypatch):
    def interrupted(unit, argv, env, log_path, props):
        scopes.states[unit] = "active"      # the scope came up just before the interrupt
        raise KeyboardInterrupt
    monkeypatch.setattr(scope, "start", interrupted)
    with pytest.raises(KeyboardInterrupt):
        launcher.launch(LaunchRequest(binary="/bin/true"))
    (sess,) = session.list_sessions()
    assert scopes.stopped == [sess.scope]


def test_interrupt_while_preparing_marks_the_session_failed(tmp_runtime, fs2, scopes,
                                                           tmp_path, monkeypatch):
    def interrupted(src, name, prefs):
        raise SystemExit(143)
    monkeypatch.setattr(launcher, "_copy_pilot", interrupted)
    with pytest.raises(SystemExit):
        launcher.launch(LaunchRequest(binary="/bin/true", pilot_source=str(tmp_path)))
    (sess,) = session.list_sessions()
    assert sess.status == "failed"


def test_sigterm_while_waiting_for_exit_stops_the_session(tmp_runtime, fs2, scopes, monkeypatch):
    def interrupted(seconds):
        raise SystemExit(143)
    monkeypatch.setattr(launcher.time, "sleep", interrupted)
    with pytest.raises(SystemExit):
        launcher.run_to_exit(LaunchRequest(binary="/bin/true"), timeout=30)
    (sess,) = session.list_sessions()
    assert scopes.stopped == [sess.scope]


# ---- cleanup vs. verbs staging side folders (fix-wave-2 R4: SL2-6 / CR2-10 / Y-3) ------------

def test_cleanup_never_removes_a_side_folder_younger_than_an_hour(tmp_runtime, scopes):
    # A verb stages its shadow folder / mission copy before its session exists, and may wait up
    # to a minute for launch.lock; `cleanup --older-than 0` deleted it in that gap.
    staged = _old(_mkdir(paths.runtime_root() / "fred-missions" / "abababab"), 1800)
    assert launcher.cleanup(0)["removed_folders"] == []
    assert staged.is_dir()


def test_cleanup_waits_for_launch_lock(tmp_runtime, scopes, monkeypatch):
    monkeypatch.setattr(launcher, "_LOCK_WAIT", 0.3)
    _mk(status="exited")
    with open(paths.runtime_root() / "launch.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        _raises(EXIT_TIMEOUT, launcher.cleanup, 0)
    assert len(launcher.cleanup(0)["removed"]) == 1


# ---- extra-argument / folder refusals, round 2 (SA2-2 / CR2-3 / Y-1) ---------------------------

@pytest.mark.parametrize("arg", ["-PORTABLE_MODE", "-Mod", "-OUTPUT_SEXPS", "-Pilot",
                                 "-get_flags", "-GET_FLAGS", "-controlconfig_tbl"])
def test_flag_checks_ignore_case_and_cover_more_writers(tmp_runtime, fs2, arg):
    # FSO matches flags with stricmp (cmdline.cpp:822), so -MOD is -mod to the engine.
    # -get_flags writes flags.lch and -controlconfig_tbl writes controlconfigdefaults.tbl into the
    # working folder.
    e = _raises(EXIT_USAGE, launcher._validate,
                LaunchRequest(binary="/bin/true", extra_args=[arg]))
    assert arg in e.message


def test_new_writers_are_allowed_in_a_shadow_folder(tmp_runtime, fs2):
    shadow = _mkdir(paths.runtime_root() / "shadows" / "abcd1234")
    launcher._validate(LaunchRequest(binary="/bin/true", cwd=str(shadow),
                                     extra_args=["-GET_FLAGS", "-controlconfig_tbl"]))


def test_a_working_folder_outside_the_runtime_root_is_refused(tmp_runtime, fs2, tmp_path):
    e = _raises(EXIT_USAGE, launcher._validate,
                LaunchRequest(binary="/bin/true", cwd=str(tmp_path)))
    assert str(tmp_path) in e.message
    launcher._validate(LaunchRequest(binary="/bin/true",
                                     cwd=str(_mkdir(paths.runtime_root() / "shadows" / "ab"))))


@pytest.mark.parametrize("mod", ["../evil", "/abs/mod", "a/b", "..", ".", ""])
def test_mod_names_must_be_plain_folder_names(tmp_runtime, fs2, mod):
    e = _raises(EXIT_USAGE, launcher._validate,
                LaunchRequest(binary="/bin/true", mods=["MVPS-4.7.3", mod]))
    assert repr(mod) in e.message


def test_capacity_counts_a_scope_until_it_is_gone(tmp_runtime, scopes, monkeypatch):
    # Y-6: a stopping ("deactivating") scope still holds its engine and Xvfb, so it still takes
    # a slot; only a gone scope frees one.
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "1")
    s = _mk(status="stopped")
    scopes.states[s.scope] = "deactivating"
    _raises(EXIT_REFUSED, launcher.check_capacity)
    scopes.states[s.scope] = "inactive"
    assert launcher.check_capacity() == []


def test_stop_still_stops_the_scope_when_cleanup_removes_the_folder_mid_stop(tmp_runtime, scopes,
                                                                            monkeypatch):
    # YK-3: the folder can vanish between session.load and the stop marker (a concurrent
    # cleanup); FileNotFoundError escaped and the live scope was never stopped.
    s = _mk(status="running")
    scopes.states[s.scope] = "active"
    real_load = session.load

    def load_then_vanish(sid):
        got = real_load(sid)
        shutil.rmtree(session.run_dir(got))
        return got
    monkeypatch.setattr(launcher.session, "load", load_then_vanish)
    r = launcher.stop(s.id)
    assert scopes.stopped == [s.scope] and r["status"] == "stopped" and r["note"]


def test_stop_reports_a_folder_removed_after_the_scope_stopped(tmp_runtime, scopes, monkeypatch):
    s = _mk(status="running")
    scopes.states[s.scope] = "active"
    real_stop = scope.stop

    def stop_then_vanish(unit, timeout=20):
        real_stop(unit, timeout)
        shutil.rmtree(session.run_dir(s))
    monkeypatch.setattr(scope, "stop", stop_then_vanish)
    r = launcher.stop(s.id)
    assert scopes.stopped == [s.scope] and r["status"] == "stopped" and r["note"]


def test_cli_cleanup_defaults_to_ten_minutes():
    # YK-5: `fsoctl cleanup` with no flag deleted a just-failed session's logs at once.
    assert cli.build_parser().parse_args(["cleanup"]).older_than == 600
