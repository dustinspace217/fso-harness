import os
import signal
import threading
import time

import pytest

from fsoctl import game, launcher, session
from fsoctl.errors import EXIT_ENGINE, EXIT_REFUSED, HarnessError
from tests.engine_helpers import launch_in_mission

pytestmark = pytest.mark.engine


@pytest.fixture
def mission(engine_env, tmp_runtime):
    _, fso = engine_env
    sid = launch_in_mission(fso)
    yield sid
    launcher.stop(sid)


def _quick(sid):
    return game.state(sid, ships=False, goals=False, events=False)


def _pitch(sid):
    return game.sexp(sid, "( get-object-pitch !Alpha 1! )", mode="number")["value"]


def _heading(sid):
    return game.sexp(sid, "( get-object-heading !Alpha 1! )", mode="number")["value"]


def test_pilot_pitch_changes_orientation_and_releases_control(mission):
    before = _pitch(mission)
    r = game.pilot(mission, pitch=1.0, forward=1.0, seconds=2.0)
    assert r["applied_frames"] > 0
    assert r["completed"] is True and r["stopped_because"] == "time"
    assert abs(_pitch(mission) - before) > 5
    assert game.call(mission, "control_mode")["mode"] == "NORMAL"   # control handed back


def test_pilot_fires_primaries(mission):
    r = game.pilot(mission, primary=True, seconds=1.0)
    assert r["shots"] > 0


def test_second_pilot_while_one_runs_is_refused(mission):
    t = threading.Thread(target=game.pilot, args=(mission,), kwargs={"seconds": 3.0})
    t.start()
    time.sleep(0.5)
    with pytest.raises(HarnessError) as e:
        game.pilot(mission, seconds=1.0)
    assert e.value.code == EXIT_REFUSED
    t.join(30)


def test_time_compression_speeds_mission_clock(mission):
    r = game.set_time(mission, compression=400)
    # Read back from the game a few frames later (QA finding SF-14), not echoed from the request.
    assert r["requested_compression"] == 400 and r["state"] == "GS_STATE_GAME_PLAY"
    assert 300 <= r["compression"] <= 500, r
    t0, w0 = _quick(mission)["mission_time"], time.monotonic()
    time.sleep(4)
    t1, w1 = _quick(mission)["mission_time"], time.monotonic()
    game.set_time(mission, compression=100)
    assert (t1 - t0) / (w1 - w0) > 2.0


def test_pause_and_resume(mission):
    r = game.set_time(mission, pause=True)
    assert r["paused"] is True and r["state"] == "GS_STATE_GAME_PAUSED"   # read back after
    assert _quick(mission)["paused"]
    t0 = _quick(mission)["mission_time"]
    time.sleep(1.5)
    assert _quick(mission)["mission_time"] == t0
    assert game.set_time(mission, resume=True)["paused"] is False
    assert not _quick(mission)["paused"]


def test_autopilot_flies_the_ship_and_hands_it_back(mission):
    # QA finding TA-6: observe the effect, not the echoed request. With the AI flying, the
    # player's orientation changes with no pilot input (probe 2026-10-12: pitch 19 -> 8 and heading
    # 353 -> 333 over 6 s); once taken back, with no input it holds still.
    before = (_pitch(mission), _heading(mission))
    assert game.autopilot(mission, True) == {"on": True, "applied": True}
    time.sleep(6)
    assert (_pitch(mission), _heading(mission)) != before
    assert game.autopilot(mission, False) == {"on": False, "applied": True}
    time.sleep(1)
    held = (_pitch(mission), _heading(mission))
    time.sleep(3)
    assert (_pitch(mission), _heading(mission)) == held


def test_pilot_is_refused_while_paused(mission):
    # QA finding SL-5: mission time stands still while paused, so a flight would never end.
    game.set_time(mission, pause=True)
    with pytest.raises(HarnessError) as e:
        game.pilot(mission, seconds=1.0)
    assert e.value.code == EXIT_REFUSED and "paused" in e.value.message
    game.set_time(mission, resume=True)


def _flying(sid) -> dict:
    """Run a 30 s flight in a thread; return {"thread", "result"} once the bridge reports it."""
    flight = {}
    t = threading.Thread(target=lambda: flight.update(result=game.pilot(sid, seconds=30.0)))
    t.start()
    flight["thread"] = t
    for _ in range(40):   # bounded: about 10 s for the flight to start
        mode = game.call(sid, "control_mode")
        if mode["flight_seq"] is not None:
            flight["seq"] = mode["flight_seq"]
            return flight
        time.sleep(0.25)
    pytest.fail(f"the flight never started; control_mode {mode}")


def test_pilot_cancel_hands_control_back(mission):
    # Contract C4 / ruling R5: pilot_cancel ends the flight it names; the flight's reply says why.
    flight = _flying(mission)
    assert game.call(mission, "control_mode")["mode"] == "FULL"
    assert game.call(mission, "pilot_cancel", {"seq": flight["seq"] + 1000}) == {
        "cancelled": False, "reason": "no matching flight"}
    assert game.call(mission, "pilot_cancel", {"seq": flight["seq"]}) == {"cancelled": True}
    flight["thread"].join(20)
    r = flight["result"]
    assert r["stopped_because"] == "cancelled" and r["completed"] is False
    assert game.call(mission, "control_mode")["mode"] == "NORMAL"
    assert game.call(mission, "pilot_cancel", {"seq": flight["seq"]})["cancelled"] is False


def test_pausing_mid_flight_ends_the_flight(mission):
    # QA finding SL2-4: mission time stops while paused; the flight ends instead of waiting.
    flight = _flying(mission)
    game.set_time(mission, pause=True)
    flight["thread"].join(20)
    try:
        assert flight["result"]["stopped_because"] == "paused"
        assert game.call(mission, "control_mode")["mode"] == "NORMAL"
    finally:
        game.set_time(mission, resume=True)


def test_clock_reports_engine_and_mission_time(mission):
    a = game.call(mission, "clock")
    time.sleep(2)
    b = game.call(mission, "clock")
    assert b["frametime_overall"] > a["frametime_overall"]
    assert b["mission_time"] > a["mission_time"]


def test_call_after_engine_killed_reports_engine(mission):
    os.kill(session.load(mission).engine_pid, signal.SIGKILL)
    for _ in range(20):   # bounded: up to 5 s for the runner to record how the engine ended
        if session.load(mission).exit_info:
            break
        time.sleep(0.25)
    t0 = time.monotonic()
    with pytest.raises(HarnessError) as e:
        game.state(mission)
    assert e.value.code == EXIT_ENGINE and "crash_report" in e.value.hint  # Review Focus 2
    assert "SIGKILL" in e.value.message   # QA finding TA-2: the signal reaches the caller
    assert time.monotonic() - t0 < 5   # an answer, not a timeout
