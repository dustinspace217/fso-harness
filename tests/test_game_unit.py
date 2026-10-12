import json
import os

import pytest

from fsoctl import channel, game, session
from fsoctl.errors import EXIT_ENGINE, EXIT_REFUSED, EXIT_USAGE, HarnessError
from tests.fake_bridge import FakeBridge


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Keep contract C7's persistent audit log out of the user's real state folder."""
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "state"))


def _sess(allow):
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=allow, extra_args=[])


def test_lua_refused_without_opt_in(tmp_runtime, monkeypatch):
    s = _sess(False)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    with pytest.raises(HarnessError) as e:
        game.lua(s.id, "return 1")
    assert e.value.code == EXIT_REFUSED and "allow-eval" in e.value.hint
    assert not (session.run_dir(s) / "audit.log").exists()


def test_lua_allowed_is_audited(tmp_runtime, monkeypatch):
    s = _sess(True)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    with FakeBridge(channel.Channel(s).dir):
        game.lua(s.id, "return 1")
    entry = json.loads((session.run_dir(s) / "audit.log").read_text().splitlines()[0])
    assert entry["kind"] == "lua" and entry["text"] == "return 1"


def test_post_event_refused_without_opt_in(tmp_runtime, monkeypatch):
    s = _sess(False)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    with pytest.raises(HarnessError) as e:
        game.post_event(s.id, "GS_EVENT_QUIT_GAME")
    assert e.value.code == EXIT_REFUSED


def test_call_on_dead_engine_is_engine_error(tmp_runtime, monkeypatch):
    # Review Focus 2: an error, not a timeout. QA TA-2: goes through the real require_running
    # (the old version stubbed it to raise exactly what the test then asserted). The pid is live,
    # so only the scope check can call the engine dead; the exit info must reach the message.
    s = _sess(False)
    s.engine_pid = os.getpid()
    s.status, s.exit_info = "exited", {"returncode": -9, "signal": "SIGKILL", "at": 0}
    session.save(s)
    monkeypatch.setattr(game.launcher.scope, "is_active", lambda unit: False)
    with pytest.raises(HarnessError) as e:
        game.state(s.id)
    assert e.value.code == EXIT_ENGINE and "SIGKILL" in e.value.message
    assert "crash_report" in e.value.hint


def test_sexp_mode_validated():
    with pytest.raises(HarnessError) as e:
        game.check_mode("maybe")
    assert e.value.code == EXIT_USAGE


@pytest.mark.parametrize("expr", [
    '( script-eval !ba.print("x")! )',
    "(script-eval-block !x=1!)",
    "( SCRIPT-EVAL-NUM !return 1! )",
    "( when ( true ) ( multi-eval !x! ( true ) ) )",
    "(\u00a0script-eval !x!)",          # whitespace the Lua pattern and Python regex disagree on
    "(\u200bscript-eval-num !return 1!)",
    "( script-eval\t!x! )",
])
def test_script_running_sexps_need_eval_opt_in(tmp_runtime, monkeypatch, expr):
    # script-eval* and multi-eval run Lua; without this check `sexp` would bypass --allow-eval
    # (found by a background security review of the Task 11 commit).
    s = _sess(False)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    with pytest.raises(HarnessError) as e:
        game.sexp(s.id, expr)
    assert e.value.code == EXIT_REFUSED and "allow-eval" in e.value.hint


def test_script_running_sexp_allowed_is_audited(tmp_runtime, monkeypatch):
    s = _sess(True)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    with FakeBridge(channel.Channel(s).dir):
        game.sexp(s.id, "( script-eval-num !return 2! )", mode="number")
    entry = json.loads((session.run_dir(s) / "audit.log").read_text().splitlines()[0])
    assert entry["kind"] == "sexp-script" and entry["text"] == "( script-eval-num !return 2! )"


def test_post_event_allowed_is_audited(tmp_runtime, monkeypatch):
    # QA MU-3: the audit line is the opt-in's only record, so its contents are pinned.
    s = _sess(True)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    with FakeBridge(channel.Channel(s).dir):
        r = game.post_event(s.id, "GS_EVENT_QUIT_GAME")
    assert r == {"echo": {"name": "GS_EVENT_QUIT_GAME"}}
    entry = json.loads((session.run_dir(s) / "audit.log").read_text().splitlines()[0])
    assert entry["kind"] == "post_event" and entry["text"] == "GS_EVENT_QUIT_GAME"


def test_plain_sexp_is_not_gated(tmp_runtime, monkeypatch):
    s = _sess(False)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    with FakeBridge(channel.Channel(s).dir):
        game.sexp(s.id, "( is-destroyed-delay 0 !Alpha 2! )")
    assert not (session.run_dir(s) / "audit.log").exists()


@pytest.mark.parametrize("kw", [{"pitch": 2.0}, {"seconds": 0}, {"seconds": 61}, {"bank": -1.5},
                                {"heading": 1.0001}, {"forward": -1.0001}, {"seconds": 0.0499},
                                {"seconds": 60.001}])
def test_pilot_arguments_validated(kw):
    with pytest.raises(HarnessError) as e:
        game.check_pilot_args(**kw)
    assert e.value.code == EXIT_USAGE and e.value.message and e.value.hint


# QA MU-5: accept rows at every boundary, so an inverted or shifted range check fails a test
# (every row above is a rejection, which an inverted check would still pass for most values).
@pytest.mark.parametrize("kw", [{"pitch": -1.0}, {"pitch": 1.0}, {"heading": -1.0},
                                {"heading": 1.0}, {"bank": -1.0}, {"bank": 1.0},
                                {"forward": -1.0}, {"forward": 1.0}, {"seconds": 0.05},
                                {"seconds": 60.0}, {}])
def test_pilot_arguments_accepted_at_the_boundaries(kw):
    game.check_pilot_args(**kw)


@pytest.mark.parametrize("kw", [{"compression": 0}, {"compression": 10000},
                                {"compression": 9.99}, {"compression": 6400.01},
                                {"pause": True, "resume": True}, {}])
def test_time_arguments_validated(kw):
    with pytest.raises(HarnessError) as e:
        game.check_time_args(**kw)
    assert e.value.code == EXIT_USAGE and e.value.message and e.value.hint


@pytest.mark.parametrize("kw", [{"compression": 10}, {"compression": 6400}, {"pause": True},
                                {"resume": True}])
def test_time_arguments_accepted_at_the_boundaries(kw):
    game.check_time_args(**kw)
