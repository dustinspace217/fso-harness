import os
import time

import pytest

from fsoctl import channel, launcher, session
from fsoctl.errors import EXIT_TIMEOUT, HarnessError
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine
MODS = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]
MISSION = os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2")


def _run(engine_env, **kw):
    _, fso = engine_env
    return launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, **kw))


def _wait_hb(sid, pred, what, timeout=180):
    """Poll the heartbeat until pred(hb) holds (bounded); fail naming the last heartbeat."""
    ch = channel.Channel(session.load(sid))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hb = ch.heartbeat() or {}
        if pred(hb):
            return hb
        time.sleep(0.5)
    pytest.fail(f"never saw {what}; last heartbeat {ch.heartbeat()}")


def _wait_state(sid, want, timeout=180):
    return _wait_hb(sid, lambda hb: hb.get("state") == want, want, timeout)


def _clean(sid):
    """Assert the bridge caught no Lua error and return its info (QA finding SF-1)."""
    info = launcher.bridge_info(sid)
    assert info["errors"] == 0, info["last_error"]
    return info


def test_create_pilot_reaches_main_menu(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", pilot_name="HTest", skip_movies=True,
             dismiss_popups=True)
    try:
        _wait_state(r["id"], "GS_STATE_MAIN_MENU")
        popups = (channel.Channel(session.load(r["id"])).dir / "popups.txt").read_text()
        assert "settings file" in popups  # auto-dismissed popups are logged, not hidden
        assert _clean(r["id"])["switch_status"] == {"pilot_mode": "done"}
    finally:
        launcher.stop(r["id"])


def test_full_skip_lands_in_gameplay(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", skip_movies=True, dismiss_popups=True,
             mission=MISSION, skip_command_briefing=True, skip_briefing=True)
    try:
        hb = _wait_state(r["id"], "GS_STATE_GAME_PLAY")
        assert hb["mission"].lower() == MISSION.lower().removesuffix(".fs2")
        assert _clean(r["id"])["switch_status"] == {
            "pilot_mode": "done", "skip_command_briefing": "done", "skip_briefing": "done"}
        hb = _wait_hb(r["id"], lambda h: h.get("switches", {}).get("skip_briefing") == "done",
                      "the heartbeat's switches", timeout=10)
        assert hb["errors"] == 0
    finally:
        launcher.stop(r["id"])


def test_no_briefing_skips_stops_at_command_briefing(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", skip_movies=True, dismiss_popups=True,
             mission=MISSION)
    try:
        _wait_state(r["id"], "GS_STATE_CMD_BRIEF")
        time.sleep(5)
        hb = channel.Channel(session.load(r["id"])).heartbeat()
        assert hb["state"] == "GS_STATE_CMD_BRIEF"  # nothing skipped it
        _clean(r["id"])
    finally:
        launcher.stop(r["id"])


# QA finding TA-11: each briefing switch on its own (spec section 5 says they are independent).

def test_skip_command_briefing_alone_stops_at_the_briefing(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", skip_movies=True, dismiss_popups=True,
             mission=MISSION, skip_command_briefing=True)
    try:
        _wait_state(r["id"], "GS_STATE_BRIEFING")
        time.sleep(5)
        assert channel.Channel(session.load(r["id"])).heartbeat()["state"] == "GS_STATE_BRIEFING"
        assert _clean(r["id"])["switch_status"]["skip_command_briefing"] == "done"
    finally:
        launcher.stop(r["id"])


def test_skip_briefing_alone_leaves_the_command_briefing_up(engine_env, tmp_runtime):
    r = _run(engine_env, pilot_mode="create", skip_movies=True, dismiss_popups=True,
             mission=MISSION, skip_briefing=True)
    try:
        _wait_state(r["id"], "GS_STATE_CMD_BRIEF")
        time.sleep(5)
        assert channel.Channel(session.load(r["id"])).heartbeat()["state"] == "GS_STATE_CMD_BRIEF"
        # The briefing switch waits for a briefing that this run never reaches.
        assert _clean(r["id"])["switch_status"]["skip_briefing"] == "pending"
    finally:
        launcher.stop(r["id"])


def test_popup_without_dismiss_is_visible_and_answerable(engine_env, tmp_runtime):
    # A fresh preferences folder shows a "new settings file" popup in pilot select (probe results
    # section 3). Without dismiss_popups it must stay up, show in the heartbeat, and close on
    # answer_popup; the pilot is then created and the game reaches the main menu.
    r = _run(engine_env, pilot_mode="create", skip_movies=True)
    try:
        hb = _wait_hb(r["id"], lambda hb: hb.get("popup"), "a popup", timeout=60)
        assert "settings file" in hb["popup"]
        ch = channel.Channel(session.load(r["id"]))
        ch.send("answer_popup", {"choice": 0}, timeout=20)
        _wait_state(r["id"], "GS_STATE_MAIN_MENU", timeout=60)
    finally:
        launcher.stop(r["id"])


def test_ready_timeout_returns_structured_error(engine_env, tmp_runtime):
    # Review Focus 1: a launch that is not ready in time returns a structured timeout and leaves
    # nothing running.
    with pytest.raises(HarnessError) as e:
        _run(engine_env, ready_timeout=0.01)
    assert e.value.code == EXIT_TIMEOUT and "screenshot" in e.value.hint
    assert session.load(e.value.details["id"]).status == "stopped"  # caller can inspect it
    assert all(not launcher.alive(s) for s in session.list_sessions())
