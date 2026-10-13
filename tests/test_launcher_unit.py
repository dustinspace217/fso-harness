import contextlib
import json
import os
import subprocess
import sys

import pytest

from fsoctl import launcher, match, paths, session
from fsoctl.errors import (EXIT_ENGINE, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE,
                           HarnessError)
from fsoctl.launcher import LaunchRequest


def _sess(**sw):
    base = dict(kind="game", binary="/x/fs2_open", fs2path="/g", mods=["MVPS"], mission="m.fs2",
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=["-seed", "5"])
    base.update(sw)
    return session.new_session(**base)


def test_argv_defaults(tmp_runtime):
    argv = launcher.build_argv(_sess())
    assert argv[:3] == ["/x/fs2_open", "-mod", f"{paths.BRIDGE_MOD},MVPS"]
    for flag in ("-window", "-nosound", "-no_unfocused_pause", "-noninteractive"):
        assert flag in argv
    assert argv[argv.index("-res") + 1] == "1280x720"
    assert argv[argv.index("-start_mission") + 1] == "m.fs2"
    assert argv[-2:] == ["-seed", "5"]
    assert "-nomovies" not in argv and "-pilot" not in argv and "-vulkan" not in argv


def test_argv_switches(tmp_runtime):
    s = _sess(renderer="vulkan", switches={"skip_movies": True, "pilot_mode": "existing",
                                           "pilot_name": "Alpha", "noninteractive": False})
    argv = launcher.build_argv(s)
    assert "-nomovies" in argv and "-vulkan" in argv and "-noninteractive" not in argv
    assert argv[argv.index("-pilot") + 1] == "Alpha"


def test_argv_qtfred_uses_full_mission_path_and_no_bridge(tmp_runtime):
    s = _sess(kind="qtfred", mission="m.fs2", mods=["MVPS"])
    argv = launcher.build_argv(s)
    assert paths.BRIDGE_MOD not in " ".join(argv)
    assert argv[argv.index("-start_mission") + 1].endswith("/m.fs2")
    assert argv[argv.index("-start_mission") + 1].startswith("/")
    # Debug qtFRED stops on a warning box at startup without it (probe results section 10).
    assert "-noninteractive" in argv


def test_session_cap_refuses_when_full(tmp_runtime, monkeypatch):
    # Nothing that decides liveness is stubbed away (QA TA-2): capacity reads the scope state.
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "1")
    monkeypatch.setattr(launcher.scope, "state", lambda unit: "active")
    s = _sess(engine_pid=os.getpid())
    s.status = "running"
    session.save(s)
    with pytest.raises(HarnessError) as e:
        launcher.check_capacity()
    assert e.value.code == EXIT_REFUSED
    assert "cap 1" in e.value.message and e.value.hint


def test_prepare_leaves_no_json_or_bin_startup_files(tmp_runtime, tmp_path, monkeypatch):
    # OpenGL startup deletes *.json and *.bin from mod cache folders (probe results section 14),
    # so everything the harness hands the bridge before launch must use other names.
    fs2 = tmp_path / "FS2"
    fs2.mkdir()
    monkeypatch.setenv("FS2PATH", str(fs2))
    monkeypatch.setattr(launcher.scope, "active_units", lambda: [])
    sess, refreshed = launcher._prepare(LaunchRequest(binary="/bin/true", skip_movies=True,
                                                      allow_eval=True))
    assert refreshed is True
    chan = paths.channel_dir(session.prefs_dir(sess))
    names = sorted(p.name for p in chan.iterdir())
    assert names == ["allow_eval", "switches.txt"]
    assert json.loads((chan / "switches.txt").read_text())["skip_movies"] is True
    assert (fs2 / paths.BRIDGE_MOD / "data" / "scripts" / "fsoharness.lua").exists()


def test_starting_session_whose_scope_died_is_not_waited_on(tmp_runtime, monkeypatch):
    s = _sess()
    monkeypatch.setattr(launcher.scope, "is_active", lambda unit: False)
    assert launcher._alive_or_starting(s.id) is False


def test_stop_is_idempotent_on_a_dead_scope(tmp_runtime, monkeypatch):
    s = _sess()
    s.status = "running"
    session.save(s)
    monkeypatch.setattr(launcher.scope, "state", lambda unit: "inactive")
    first = launcher.stop(s.id)
    second = launcher.stop(s.id)
    assert first["status"] == second["status"] == "stopped"


# ---- multiplayer roles (plan Task 5) --------------------------------------------------------

def _match_alive(monkeypatch, mid="m0a1b2c3d"):
    """Pretend `mid` exists and is alive without a holder."""
    m = match.Match(id=mid, scope=match.scope_name(mid), holder_pid=1, holder_start=1,
                    status="alive")

    def fake_load(x):
        if x == mid:
            return m
        raise HarnessError(EXIT_NOT_FOUND, f"no match {x}", "use `fsoctl match list`")
    monkeypatch.setattr(match, "load", fake_load)
    monkeypatch.setattr(match, "alive", lambda x: x.id == mid)
    return m


def test_argv_standalone(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(mission=None, role="standalone", match="m0a1b2c3d", port=7808, extra_args=[])
    argv = launcher.build_argv(s)
    assert "-standalone" in argv and argv[argv.index("-port") + 1] == "7808"
    for flag in ("-window", "-res", "-connect", "-startgame", "-almission"):
        assert flag not in argv
    assert "-nosound" in argv and "-noninteractive" in argv
    assert argv[1:3] == ["-mod", f"{paths.BRIDGE_MOD},MVPS"]


def test_argv_host_and_client(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    h = _sess(mission="Coop-Long.fs2", role="host", match="m0a1b2c3d", port=7808, game_name="g",
              closed=True, extra_args=[])
    argv = launcher.build_argv(h)
    assert "-startgame" in argv and "-closed" in argv
    assert argv[argv.index("-gamename") + 1] == "g"
    assert argv[argv.index("-port") + 1] == "7808"
    assert argv[argv.index("-almission") + 1] == "Coop-Long"   # the name, no .fs2
    assert "-start_mission" not in argv and "-window" in argv
    c = _sess(mission=None, role="client", match="m0a1b2c3d", port=7809,
              connect="127.0.0.1:7808", extra_args=[])
    argv = launcher.build_argv(c)
    assert argv[argv.index("-connect") + 1] == "127.0.0.1:7808"
    assert argv[argv.index("-port") + 1] == "7809" and "-startgame" not in argv
    assert "-gamename" not in argv


def test_multi_cfg_never_enables_tracker():
    text = launcher.multi_cfg_text("g")
    assert "+name g" in text and "+pxo" not in text and "+webapi_server_port 8080" in text


def test_role_validation(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    base = dict(binary="/bin/true")
    cases = ((dict(role="client"), EXIT_USAGE),                        # role without match
             (dict(match="m0a1b2c3d"), EXIT_USAGE),                    # match without role
             (dict(match="m0a1b2c3d", role="server"), EXIT_USAGE),     # bad role
             (dict(match="m0a1b2c3d", role="client", kind="qtfred"), EXIT_USAGE),
             (dict(match="m0a1b2c3d", role="standalone", mission="m-01.fs2"), EXIT_USAGE),
             (dict(match="m00000000", role="client", pilot_mode="create"), EXIT_NOT_FOUND))
    for bad, code in cases:
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(**base, **bad))
        assert e.value.code == code, bad
    monkeypatch.setattr(match, "alive", lambda x: False)
    with pytest.raises(HarnessError) as e:
        launcher._validate(LaunchRequest(**base, match="m0a1b2c3d", role="client",
                                         pilot_mode="create"))
    assert e.value.code == EXIT_REFUSED


def test_assign_port_skips_ports_used_by_live_sessions_only(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    srv = _sess(role="standalone", match="m0a1b2c3d", port=7808)
    _sess(role="client", match="m0a1b2c3d", port=7809)
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: False)      # all live
    assert launcher.assign_port("m0a1b2c3d", None) == 7810
    with pytest.raises(HarnessError) as e:
        launcher.assign_port("m0a1b2c3d", 7809)
    assert e.value.code == EXIT_USAGE
    assert launcher.assign_port("mffffffff", None) == 7808   # an empty match starts at the default
    # QA SL-1/TA-3: a stopped server's port is free again, and a client's default connect follows
    # the LIVE server, not a constant.
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: unit == srv.scope)
    assert launcher.assign_port("m0a1b2c3d", None) == 7808
    assert launcher.assign_port("m0a1b2c3d", 7808) == 7808
    assert launcher.default_connect("m0a1b2c3d") == "127.0.0.1:7808"      # no live server yet
    _sess(role="standalone", match="m0a1b2c3d", port=7810)
    assert launcher.default_connect("m0a1b2c3d") == "127.0.0.1:7810"


def test_prepare_writes_multi_cfg_and_switches(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    game = tmp_runtime.parent / "FS2"       # fs2path() wants an existing folder
    game.mkdir()
    monkeypatch.setenv("FS2PATH", str(game))
    req = LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone")
    sess, _ = launcher._prepare(req)
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: False)   # the standalone is live
    cfg = (session.prefs_dir(sess) / "data" / "multi.cfg").read_text()
    assert cfg.startswith("+name m0a1b2c3d")
    assert sess.port == 7808 and sess.role == "standalone" and sess.match == "m0a1b2c3d"
    req = LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client", pilot_mode="create")
    sess, _ = launcher._prepare(req)
    sw = json.loads((paths.channel_dir(session.prefs_dir(sess)) / "switches.txt").read_text())
    assert sw["pilot_multi"] is True and sw["multi_role"] == "client"
    assert sess.port == 7809 and sess.connect == "127.0.0.1:7808"     # the live standalone
    cfg = (session.prefs_dir(sess) / "data" / "multi.cfg").read_text()  # every role (QA SA-8)
    assert "+pxo" not in cfg


def test_start_wraps_runner_in_nsenter_for_match_sessions(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    monkeypatch.setattr(match, "nsenter_prefix", lambda m: ["nsenter", "--target", "1", "--"])
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    game = tmp_runtime.parent / "FS2"
    game.mkdir()
    monkeypatch.setenv("FS2PATH", str(game))
    seen = {}

    def fake_start(unit, argv, env, log, props):
        seen["argv"] = argv
    monkeypatch.setattr(launcher.scope, "start", fake_start)
    monkeypatch.setattr(launcher, "check_capacity", lambda: [])
    launcher._start(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client",
                                  pilot_mode="create"))
    assert seen["argv"][:4] == ["nsenter", "--target", "1", "--"]
    assert seen["argv"][4:7] == [sys.executable, "-m", "fsoctl.runner"]
    seen.clear()
    launcher._start(LaunchRequest(binary="/bin/true"))
    assert seen["argv"][0] == sys.executable          # ordinary sessions are not wrapped


def test_multiplayer_values_cannot_inject_flags_or_config(tmp_runtime, monkeypatch):
    # The engine joins argv back into one string and re-splits it (cmdline.cpp), and game_name
    # also lands in multi.cfg, so whitespace or newlines in these values would add flags or
    # config lines (background security review of ecb6b80).
    _match_alive(monkeypatch)
    base = dict(binary="/bin/true", match="m0a1b2c3d")
    bad = (dict(role="standalone", game_name="x\n+pxo"),
           dict(role="host", game_name="x -pilot y"),
           dict(role="client", connect="127.0.0.1:7808 -nosound"),
           dict(role="client", connect="example.com:7808"),
           dict(role="host", mission="m-01 -x"),
           dict(role="host", mission="../m-01"))
    for kw in bad:
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(**base, pilot_mode="create", **kw))
        assert e.value.code == EXIT_USAGE, kw
    launcher._validate(LaunchRequest(**base, role="host", game_name="Repro_1", mission="M-01.fs2",
                                     connect="[::1]:7808", pilot_mode="create"))


# ---- readiness and status per role (plan Task 7) -------------------------------------------

def test_listening_asks_inside_the_match(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    calls = []

    def fake_run(mm, argv, timeout=10):
        assert mm.id == "m0a1b2c3d"
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="UNCONN 0 0 *:7808 *:*\n", stderr="")
    monkeypatch.setattr(match, "run_inside", fake_run)
    assert launcher.listening(s) is True
    assert calls[0] == ["ss", "-H", "-lun", "sport = :7808"]
    monkeypatch.setattr(match, "run_inside",
                        lambda mm, argv, timeout=10: subprocess.CompletedProcess(argv, 0, "", ""))
    assert launcher.listening(s) is False


def test_wait_ready_standalone_times_out_with_timeout_code(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    s.status = "running"
    session.save(s)
    monkeypatch.setattr(launcher, "listening", lambda sess: False)
    req = LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone",
                        ready_timeout=0.3)
    with pytest.raises(HarnessError) as e:
        launcher._wait_ready(req, s.id)
    assert e.value.code == EXIT_TIMEOUT and "7808" in e.value.message


def test_status_reports_dead_network(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="client", match="m0a1b2c3d", port=7809, mission=None, extra_args=[])
    s.status = "running"
    session.save(s)
    monkeypatch.setattr(launcher.channel.Channel, "heartbeat", lambda self: {})
    monkeypatch.setattr(launcher, "alive", lambda sess: True)
    assert launcher.status(s.id)["network"] == "alive"
    monkeypatch.setattr(match, "alive", lambda m: False)
    st = launcher.status(s.id)
    assert st["network"] == "dead" and st["status"] == "dead-network"
    plain = _sess()
    assert launcher.status(plain.id)["network"] is None
    assert launcher.list_all()["sessions"][0]["role"] == "client"


# ---- match lifetime (plan Task 8) -----------------------------------------------------------

def test_remove_match_stops_starting_sessions_first(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="client", match="m0a1b2c3d", port=7809, mission=None, extra_args=[])
    assert s.status == "starting"
    r = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    r.status = "running"
    session.save(r)
    done = _sess(role="client", match="m0a1b2c3d", port=7810, mission=None, extra_args=[])
    done.status = "exited"
    session.save(done)
    order = []
    monkeypatch.setattr(launcher, "stop",
                        lambda sid: order.append(("stop", sid)) or {"status": "stopped"})
    monkeypatch.setattr(match, "end_holder", lambda m: order.append(("holder", m.id)))
    out = launcher.remove_match("m0a1b2c3d")
    assert order == [("stop", s.id), ("stop", r.id), ("holder", "m0a1b2c3d")]
    assert out["stopped"] == [s.id, r.id] and out["status"] == "removed"
    assert out["id"] == "m0a1b2c3d"


def test_cleanup_keeps_match_with_sessions_and_removes_dead_empty_one(tmp_runtime, monkeypatch):
    m = _match_alive(monkeypatch)
    match.save(m)
    s = _sess(role="client", match="m0a1b2c3d", port=7809, mission=None, extra_args=[])
    s.status = "exited"
    session.save(s)
    monkeypatch.setattr(match, "alive", lambda mm: False)
    monkeypatch.setattr(launcher, "_removable", lambda sess, now, older: False)   # keep the session
    out = launcher.cleanup(0)
    assert "m0a1b2c3d" not in out["removed"] and paths.match_dir("m0a1b2c3d").exists()
    monkeypatch.setattr(launcher, "_removable", lambda sess, now, older: True)
    out = launcher.cleanup(0)
    assert s.id in out["removed"] and "m0a1b2c3d" in out["removed"]
    assert not paths.match_dir("m0a1b2c3d").exists()


def test_cleanup_never_removes_a_live_match(tmp_runtime, monkeypatch):
    m = _match_alive(monkeypatch)
    match.save(m)
    out = launcher.cleanup(0)
    assert "m0a1b2c3d" not in out["removed"] and paths.match_dir("m0a1b2c3d").exists()


def test_wait_listening_returns_when_listening_and_fails_when_exited(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    s = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    req = LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone",
                        ready_timeout=5)
    seen = []     # the session's status at each `listening` call: never asked before "running"
    monkeypatch.setattr(launcher, "listening",
                        lambda sess: seen.append(session.load(sess.id).status) or True)
    s.status = "starting"
    session.save(s)
    monkeypatch.setattr(launcher.time, "sleep", lambda n: session.update(
        s.id, lambda x: setattr(x, "status", "running")))
    launcher._wait_listening(req, s.id)
    assert seen == ["running"]
    for ended in ("exited", "failed"):
        s = session.load(s.id)
        s.status = ended
        session.save(s)
        with pytest.raises(HarnessError) as e:
            launcher._wait_listening(req, s.id)
        assert e.value.code == EXIT_ENGINE, ended


# ---- QA fix wave, batch 1: validation (SA-1, SA-2, SA-4, TA-7, CR-8, CR-11, XFA-5, SL-9, TA-10)

def test_mission_name_cannot_start_with_a_dash_on_any_session(tmp_runtime, monkeypatch):
    # SA-1: the engine parses a value that starts with "-" as its own flag (cmdline.cpp:745-749),
    # so `--mission -portable_mode` would pass -almission/-start_mission and then act as a flag.
    _match_alive(monkeypatch)
    for kw in (dict(), dict(match="m0a1b2c3d", role="host")):
        for bad in ("-portable_mode", ".hidden", "-nosound"):
            with pytest.raises(HarnessError) as e:
                launcher._validate(LaunchRequest(binary="/bin/true", mission=bad, **kw))
            assert e.value.code == EXIT_USAGE, (kw, bad)
    launcher._validate(LaunchRequest(binary="/bin/true", mission="M-01.fs2"))


def test_multiplayer_engine_flags_are_refused_as_extra_args(tmp_runtime):
    # SA-2: these would start a server or host on the REAL network from an ordinary session.
    for flag in ("-standalone", "-startgame", "-connect", "-port", "-gamename", "-closed",
                 "-almission", "-gateway_ip", "-STANDALONE"):
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(binary="/bin/true", extra_args=[flag, "x"]))
        assert e.value.code == EXIT_USAGE and "--match" in e.value.hint, flag


def test_port_range_and_flags_without_role(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    for port in (0, 1000, 1023, 65536, 70000):           # SA-4: the engine remaps <= 1023 to 7808
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client",
                                             pilot_mode="create", port=port))
        assert e.value.code == EXIT_USAGE, port
    for kw in (dict(port=7809), dict(connect="127.0.0.1:7808"), dict(game_name="g"),
               dict(closed=True)):                         # TA-7: silently dropped before
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(binary="/bin/true", **kw))
        assert e.value.code == EXIT_USAGE, kw


def test_multiplayer_roles_need_a_created_pilot_and_refuse_vulkan_standalone(tmp_runtime,
                                                                                monkeypatch):
    _match_alive(monkeypatch)
    for role in ("host", "client"):                        # CR-8: chooser/existing cannot work
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role=role))
        assert e.value.code == EXIT_USAGE and "create:" in e.value.hint, role
    launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client",
                                     pilot_mode="create"))
    with pytest.raises(HarnessError) as e:                 # XFA-5
        launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone",
                                         renderer="vulkan"))
    assert e.value.code == EXIT_USAGE
    with pytest.raises(HarnessError) as e:                 # SL-9: holder lives 24 h
        launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone",
                                         runtime_sec=match.HOLDER_RUNTIME_SEC + 1))
    assert e.value.code == EXIT_USAGE


def test_match_roles_refuse_root(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    monkeypatch.setattr(os, "getuid", lambda: 0)          # TA-10
    with pytest.raises(HarnessError) as e:
        launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="standalone"))
    assert e.value.code == EXIT_REFUSED


def test_almission_strip_is_case_insensitive(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)                              # CR-11
    s = _sess(mission="M-01.FS2", role="host", match="m0a1b2c3d", port=7808, extra_args=[])
    argv = launcher.build_argv(s)
    assert argv[argv.index("-almission") + 1] == "M-01"


def test_remove_match_marks_removing_under_a_short_lock_and_survives_a_stop_failure(
        tmp_runtime, monkeypatch):
    # SL-4 / CR-1 / TA-8: the critical section (status "removing", list sessions) runs under
    # launch.lock; the stops run OUTSIDE it (a stop can take 30 s); SA-6 / SL-12: a session that
    # refuses to stop is reported and the holder is still ended.
    m = _match_alive(monkeypatch)
    match.save(m)
    a = _sess(role="client", match="m0a1b2c3d", port=7809, mission=None, extra_args=[])
    b = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    events = []
    real_lock = session.file_lock

    @contextlib.contextmanager
    def spy_lock(path, timeout):
        events.append(("lock", path.name))
        with real_lock(path, timeout):
            yield
        events.append(("unlock", path.name))
    monkeypatch.setattr(session, "file_lock", spy_lock)

    def fake_stop(sid):
        events.append(("stop", sid, match.load("m0a1b2c3d").status))
        if sid == a.id:
            raise HarnessError(EXIT_ENGINE, "scope survived SIGKILL", "")
        return {"status": "stopped"}
    monkeypatch.setattr(launcher, "stop", fake_stop)
    monkeypatch.setattr(match, "end_holder", lambda mm: events.append(("holder", mm.id)))
    out = launcher.remove_match("m0a1b2c3d")
    kinds = [e[0] for e in events]
    assert kinds.index("unlock") < kinds.index("stop")           # stops outside the lock
    assert [e for e in events if e[0] == "stop"] == [("stop", a.id, "removing"),
                                                    ("stop", b.id, "removing")]
    assert events[-1] == ("holder", "m0a1b2c3d")
    assert out["stopped"] == [b.id]
    assert out["failed"] == [{"id": a.id, "error": "scope survived SIGKILL"}]


# ---- QA fix wave, batch 3: one server and one mission per match, match cap, multi.cfg for all
# roles, the multi_launch switch (SL-3, XFK-2, XFK-3, XFK-5, SA-8)

def test_one_server_and_one_mission_per_match(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    asked = []
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: asked.append(unit) or False)
    srv = _sess(role="standalone", match="m0a1b2c3d", port=7808, mission=None, extra_args=[])
    for role in ("standalone", "host"):            # XFK-2: the web panel port is per match
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role=role,
                                             pilot_mode="create"))
        assert e.value.code == EXIT_USAGE and "server" in e.value.message, role
    assert srv.scope in asked                      # liveness asked about the real scope
    _sess(role="client", match="m0a1b2c3d", port=7809, mission="M-01.fs2", extra_args=[])
    with pytest.raises(HarnessError) as e:         # XFK-5: only one session carries -almission
        launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client",
                                         pilot_mode="create", mission="M-02.fs2"))
    assert e.value.code == EXIT_USAGE and "mission" in e.value.message
    launcher._validate(LaunchRequest(binary="/bin/true", match="m0a1b2c3d", role="client",
                                     pilot_mode="create"))


def test_multi_launch_switch_and_multi_cfg_for_every_role(tmp_runtime, monkeypatch):
    _match_alive(monkeypatch)
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    game = tmp_runtime.parent / "FS2"
    game.mkdir()
    monkeypatch.setenv("FS2PATH", str(game))
    sess, _ = launcher._prepare(LaunchRequest(binary="/bin/true", match="m0a1b2c3d",
                                              role="client", pilot_mode="create",
                                              mission="M-01.fs2"))
    sw = json.loads((paths.channel_dir(session.prefs_dir(sess)) / "switches.txt").read_text())
    assert sw["multi_launch"] is True                 # SL-3: only this session presses Launch
    assert (session.prefs_dir(sess) / "data" / "multi.cfg").exists()     # SA-8
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: False)
    sess2, _ = launcher._prepare(LaunchRequest(binary="/bin/true", match="m0a1b2c3d",
                                               role="client", pilot_mode="create"))
    sw2 = json.loads((paths.channel_dir(session.prefs_dir(sess2)) / "switches.txt").read_text())
    assert sw2["multi_launch"] is False
