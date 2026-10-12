import json
import os

import pytest

from fsoctl import launcher, paths, session
from fsoctl.errors import EXIT_REFUSED, HarnessError
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
