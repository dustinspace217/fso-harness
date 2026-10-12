import pytest

from fsoctl import channel, game, launcher, session
from fsoctl.errors import EXIT_BRIDGE, EXIT_REFUSED, HarnessError
from fsoctl.launcher import LaunchRequest
from tests.engine_helpers import MODS, launch_in_mission

pytestmark = pytest.mark.engine


@pytest.fixture
def in_mission(engine_env, tmp_runtime):
    _, fso = engine_env
    sid = launch_in_mission(fso, allow_eval=True)
    yield sid
    launcher.stop(sid)


def test_state_lists_player_ships_goals_events(in_mission):
    st = game.state(in_mission)
    assert st["state"] == "GS_STATE_GAME_PLAY"
    assert st["player"]["name"] == "Alpha 1" and st["player"]["hull_pct"] > 0
    assert any(s["name"] == st["player"]["name"] for s in st["ships"])
    assert len(st["events"]) == 62 and len(st["goals"]) == 1   # SM1-01 (probe results section 5)
    assert all(e["status"] in ("true", "false", "pending") for e in st["events"])
    assert st["goals"][0]["status"] in ("complete", "failed", "incomplete")


def test_sexp_eval_and_number(in_mission):
    assert game.sexp(in_mission, "( true )")["value"] is True
    assert game.sexp(in_mission, "( + 2 3 )", mode="number")["value"] == 5


def test_lua_eval_when_allowed(in_mission):
    assert game.lua(in_mission, "return 6*7")["value"] == "42"


def test_lua_error_is_reported_not_raised_in_engine(in_mission):
    with pytest.raises(HarnessError) as e:
        game.lua(in_mission, "error('boom')")
    # A Lua error in the caller's code is a bridge script error (exit 8), not a refusal (QA SF-18).
    assert e.value.code == EXIT_BRIDGE and "boom" in e.value.message
    assert game.state(in_mission, ships=False, goals=False, events=False)["state"]  # still alive


def test_bridge_refusal_keeps_engine_alive(in_mission):
    # Refusals inside the bridge must never go through FSO's error() forwarder, which is fatal
    # (probe results section 15).
    with pytest.raises(HarnessError) as e:
        game.answer_popup(in_mission, 0)
    assert e.value.code == EXIT_REFUSED and "no popup is open" in e.value.message
    assert game.state(in_mission, ships=False, goals=False, events=False)["state"]


def test_bridge_refuses_script_sexp_without_eval(engine_env, tmp_runtime):
    # Defence in depth: even a caller that skips fsoctl.game and writes the command file directly
    # cannot run Lua through a sexp on a session launched without --allow-eval.
    _, fso = engine_env
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, skip_movies=True))
    try:
        ch = channel.Channel(session.load(r["id"]))
        with pytest.raises(HarnessError) as e:
            ch.send("sexp", {"expr": "( script-eval-num !return 1! )", "mode": "number"},
                    timeout=10)
        assert e.value.code == EXIT_REFUSED and "runs Lua" in e.value.message
        assert launcher.alive(session.load(r["id"]))
    finally:
        launcher.stop(r["id"])


def test_sexp_outside_a_mission_is_refused(engine_env, tmp_runtime):
    # Outside a mission the engine evaluates even ( true ) as false (checked 2026-10-12), so a
    # result there would be silently wrong; the bridge refuses instead.
    _, fso = engine_env
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, skip_movies=True))
    try:
        with pytest.raises(HarnessError) as e:
            game.sexp(r["id"], "( true )")
        assert e.value.code == EXIT_REFUSED and "mission" in e.value.message
    finally:
        launcher.stop(r["id"])
