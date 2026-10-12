"""Group D fix-wave tests for validate (QA SF-7, TA-14), with the engine faked."""
import json

import pytest

from fsoctl import channel, game, launcher, logs, session, validate
from fsoctl.errors import EXIT_ENGINE, EXIT_TIMEOUT, EXIT_USAGE, HarnessError


@pytest.fixture
def env(tmp_path, tmp_runtime, monkeypatch):
    """FS2PATH with one mod folder, a real session record, and launch/stop/game faked."""
    fs2 = tmp_path / "fs2"
    (fs2 / "ModA").mkdir(parents=True)
    monkeypatch.setenv("FS2PATH", str(fs2))
    s = session.new_session(kind="game", binary="/bin/true", fs2path=str(fs2), mods=["ModA"],
                            mission=None, renderer="opengl", width=1, height=1, switches={},
                            allow_eval=False, extra_args=[])
    box = {"stopped": [], "launched": 0, "alive": True}

    def launch(req):
        box["launched"] += 1
        return {"id": s.id}
    monkeypatch.setattr(launcher, "launch", launch)
    monkeypatch.setattr(launcher, "stop", box["stopped"].append)
    monkeypatch.setattr(launcher, "alive", lambda sess: box["alive"])
    monkeypatch.setattr(game, "wait_for", lambda *a, **k: {"met": True})
    monkeypatch.setattr(game, "call", lambda sid, cmd, *a, **k: {"warnings": 0, "errors": 0})
    box["sess"] = s
    return box


def _heartbeat(s, state: str) -> None:
    """Write the bridge heartbeat a running bridge would have left."""
    d = channel.Channel(s).dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "ready.json").write_text(json.dumps({"state": state}))


def test_engine_death_during_launch_is_an_aborted_finding(env, monkeypatch):
    s = env["sess"]
    _heartbeat(s, "GS_STATE_INITIAL_PLAYER_SELECT")

    def launch(req):
        raise HarnessError(EXIT_ENGINE, "engine exited before the bridge started",
                           "run `fsoctl crash_report <id>`", {"id": s.id})
    monkeypatch.setattr(launcher, "launch", launch)
    env["alive"] = False
    r = validate.validate(["ModA"], binary="/bin/true")
    assert r["aborted"] is True and r["reached"] is None and r["id"] == s.id
    assert r["abort_reason"] == {"kind": "engine_died",
                                 "message": "engine exited before the bridge started",
                                 "last_state": "GS_STATE_INITIAL_PLAYER_SELECT"}
    assert env["stopped"] == [s.id]


def test_timeout_is_told_apart_from_engine_death(env, monkeypatch):
    s = env["sess"]
    _heartbeat(s, "GS_STATE_MAIN_MENU")

    def wait_for(*a, **k):
        raise HarnessError(EXIT_TIMEOUT, "state did not reach GS_STATE_GAME_PLAY", "screenshot")
    monkeypatch.setattr(game, "wait_for", wait_for)
    r = validate.validate(["ModA"], mission="m.fs2", binary="/bin/true")
    assert r["aborted"] is True
    assert r["abort_reason"]["kind"] == "timeout"
    assert r["abort_reason"]["last_state"] == "GS_STATE_MAIN_MENU"
    assert "GS_STATE_GAME_PLAY" in r["abort_reason"]["message"]


def test_readiness_timeout_inside_launch_stays_a_timeout(env, monkeypatch):
    # CR2-4 / Y-8: launch stops the session before it raises its readiness EXIT_TIMEOUT, so the
    # engine is already gone when validate looks; the error code, not liveness, decides.
    s = env["sess"]

    def launch(req):
        raise HarnessError(EXIT_TIMEOUT, "no bridge heartbeat after 240s", "screenshot",
                           {"id": s.id})
    monkeypatch.setattr(launcher, "launch", launch)
    env["alive"] = False
    r = validate.validate(["ModA"], binary="/bin/true")
    assert r["abort_reason"]["kind"] == "timeout"
    assert r["abort_reason"]["last_state"] is None   # no heartbeat was ever written


def test_missing_mod_folder_is_refused_before_launch(env):
    with pytest.raises(HarnessError) as e:
        validate.validate(["ModA", "NoSuchMod"], binary="/bin/true")
    assert e.value.code == EXIT_USAGE and "NoSuchMod" in e.value.message and e.value.hint
    assert env["launched"] == 0


def test_release_build_reports_null_counts(env):
    r = validate.validate(["ModA"], binary="/bin/true")
    assert r["build"] == "release" and r["aborted"] is False and r["abort_reason"] is None
    assert r["warnings"] is None and r["errors"] is None
    assert r["note"].startswith("release build: Warning() is compiled out")


def test_debug_build_reports_the_engine_counts(env, monkeypatch):
    s = env["sess"]
    log = logs.sources(s)["fso"]
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('WARNING: "bad" at x.cpp:1\n')
    monkeypatch.setattr(game, "call", lambda sid, cmd, *a, **k: {"warnings": 3, "errors": 0})
    r = validate.validate(["ModA"], binary="/bin/true")
    assert r["build"] == "debug" and r["warnings"] == 3 and r["errors"] == 0
    assert r["problems_total"] == 1 and "note" not in r
