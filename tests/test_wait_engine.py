import pytest

from fsoctl import game, launcher
from fsoctl.errors import EXIT_TIMEOUT, HarnessError
from tests.engine_helpers import launch_in_mission

pytestmark = pytest.mark.engine


@pytest.fixture
def mission(engine_env, tmp_runtime):
    _, fso = engine_env
    sid = launch_in_mission(fso)
    yield sid
    launcher.stop(sid)


def test_wait_for_mission_time(mission):
    r = game.wait_for(mission, "mission_time", 5, timeout=60)
    assert r["met"] and r["detail"] >= 5


def test_wait_for_event_that_becomes_true(mission):
    # In SM1-01, "Addressing Transports" turns true between 5 and 20 mission seconds (observed
    # 2026-10-12 with every startup stage skipped); it is not true at mission start. Check that
    # first, so an event that is already true cannot pass this test (QA finding TA-12).
    first = game.call(mission, "check", {"kind": "event_true", "value": "Addressing Transports"})
    assert first == {"met": False, "detail": "pending"}
    r = game.wait_for(mission, "event_true", "Addressing Transports", timeout=120)
    assert r["met"] and r["after_sec"] > 0


def test_wait_for_goal_status(mission):
    assert game.wait_for(mission, "goal", "Protect Iota=incomplete", timeout=10)["met"]


def test_wait_timeout_names_the_last_detail(mission):
    with pytest.raises(HarnessError) as e:
        game.wait_for(mission, "state", "GS_STATE_BRIEFING", timeout=2)
    assert e.value.code == EXIT_TIMEOUT and "GS_STATE_GAME_PLAY" in e.value.message


def test_wait_for_unknown_goal_is_refused(mission):
    with pytest.raises(HarnessError) as e:
        game.wait_for(mission, "goal", "No Such Goal=complete", timeout=5)
    assert "no goal named" in e.value.message
