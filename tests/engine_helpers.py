"""Shared helpers for engine-tier tests: launch into a mission and wait on the bridge heartbeat."""
import os
import time

import pytest

from fsoctl import channel, launcher, session
from fsoctl.launcher import LaunchRequest

MODS = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]
MISSION = os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2")


def wait_hb(sid: str, pred, what: str, timeout: float = 180) -> dict:
    """Poll the heartbeat until pred(heartbeat) is true (bounded); fail naming the last one."""
    ch = channel.Channel(session.load(sid))
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        hb = ch.heartbeat() or {}
        if pred(hb):
            return hb
        time.sleep(0.5)
    pytest.fail(f"never saw {what}; last heartbeat {ch.heartbeat()}")


def launch_in_mission(fso_bin, **kw) -> str:
    """Launch with every stage skipped into the test mission; return the id once in gameplay."""
    r = launcher.launch(LaunchRequest(binary=str(fso_bin), mods=MODS, mission=MISSION,
                                      pilot_mode="create", skip_movies=True, dismiss_popups=True,
                                      skip_command_briefing=True, skip_briefing=True, **kw))
    # Stop the session if gameplay never arrives (or the run is interrupted): the fixtures that
    # call this only stop it after their yield, which a failure here never reaches, and the
    # leaked engine would hold a capacity slot (QA findings TA2-6 / Y-7).
    try:
        wait_hb(r["id"], lambda hb: hb.get("state") == "GS_STATE_GAME_PLAY", "GS_STATE_GAME_PLAY")
    except BaseException:
        launcher.stop(r["id"])
        raise
    return r["id"]
