import os
import time

import pytest

from fsoctl import game, launcher, session, xinput
from fsoctl.launcher import LaunchRequest
from tests.engine_helpers import launch_in_mission, wait_hb

pytestmark = pytest.mark.engine
# Pillow is a test-only dependency (python3-pillow); without it this module is skipped, so tools
# that run the unit tier in their own environment (mutmut) still collect cleanly.
Image = pytest.importorskip("PIL.Image")
ImageChops = pytest.importorskip("PIL.ImageChops")
ImageStat = pytest.importorskip("PIL.ImageStat")


def test_screenshot_is_the_game_window_and_not_blank(engine_env, tmp_runtime):
    _, fso = engine_env
    mods = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=mods, pilot_mode="create",
                                      skip_movies=True, dismiss_popups=True,
                                      width=1024, height=768))
    try:
        shot = xinput.screenshot(r["id"])
        assert (shot["width"], shot["height"]) == (1024, 768)
        im = Image.open(shot["path"]).convert("L")
        assert im.getextrema()[1] > 40   # not a black frame (launch returns at pilot select)
        small = xinput.screenshot(r["id"], scale=0.5)
        assert small["width"] == 512
        crop = xinput.screenshot(r["id"], crop="200x100+10+20")
        assert (crop["width"], crop["height"]) == (200, 100)
        # Input calls must reach the session display without error (effects are checked by later
        # tasks through game state, where an effect can be asserted).
        assert xinput.keys(r["id"], ["Escape"])["pressed"] == ["Escape"]
        assert xinput.click(r["id"], 5, 5)["clicked"] == [5, 5]
        assert xinput.scroll(r["id"], 5, 5, -2)["scrolled"] == -2
    finally:
        launcher.stop(r["id"])


# ---- SM1-01 demo findings, checked on the real engine (2026-10-12) ---------------------------

def _diff(a, b) -> float:
    """Mean absolute grey-level difference of two same-size images (0-255)."""
    return ImageStat.Stat(ImageChops.difference(a.convert("L"), b.convert("L"))).mean[0]


def _panel_drawn(sid: str, crop: str, timeout: float = 20.0):
    """Capture `crop` until it shows something (the debriefing fades in), bounded; return it."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        im = Image.open(xinput.screenshot(sid, crop=crop)["path"])
        if im.convert("L").getextrema()[1] > 40:
            return im
        time.sleep(0.5)
    pytest.fail(f"the debriefing panel never drew within {timeout:.0f} s")


def test_held_click_works_on_fso_ui_screens(engine_env, tmp_runtime):
    # FSO's UI samples the button once per frame; a sub-frame click never registered on the
    # debriefing's Statistics tab. The held click must switch the tab: the text panel changes.
    _, fso = engine_env
    sid = launch_in_mission(fso, allow_eval=True)
    try:
        game.lua(sid, "return tostring(hv.Player:warpOut())")
        wait_hb(sid, lambda hb: hb.get("state") == "GS_STATE_DEBRIEF", "GS_STATE_DEBRIEF", 60)
        panel = "560x400+200+200"          # the debriefing text box at 1280x720
        _panel_drawn(sid, panel)           # input before the fade-in ends is ignored
        time.sleep(1.0)
        before = Image.open(xinput.screenshot(sid, crop=panel)["path"])
        time.sleep(0.5)
        settled = Image.open(xinput.screenshot(sid, crop=panel)["path"])
        assert _diff(before, settled) < 0.5    # the fade is over: a change now is the click (TA-1)
        xinput.click(sid, 185, 46)         # "Statistics" tab button
        time.sleep(0.5)
        after = Image.open(xinput.screenshot(sid, crop=panel)["path"])
        assert _diff(before, after) > 1.0
    finally:
        launcher.stop(sid)


def test_stop_lets_the_engine_exit_cleanly(engine_env, tmp_runtime):
    # Before the runner-first stop, every stop ended in SIGABRT (Xvfb went away under a running
    # engine) and left a core dump. The recorded exit must now be a plain exit, not a signal.
    _, fso = engine_env
    mods = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=mods, pilot_mode="create",
                                      skip_movies=True, dismiss_popups=True))
    out = launcher.stop(r["id"])
    assert out["status"] == "stopped"
    assert out["exit_info"] and out["exit_info"]["signal"] is None, out["exit_info"]


def test_move_puts_the_pointer_where_it_says(engine_env, tmp_runtime):
    _, fso = engine_env
    mods = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=mods, pilot_mode="create",
                                      skip_movies=True, dismiss_popups=True))
    try:
        sess = launcher.require_running(r["id"])
        ox, oy = xinput.window_origin(sess)
        # The game window sits below the display's top edge (probed: 0,40), so a window-relative
        # move and an absolute one land on different points (QA TA-10).
        assert oy != 0, (ox, oy)
        assert xinput.move(r["id"], 300, 200)["pointer"] == [ox + 300, oy + 200]
        assert xinput.move(r["id"], 10, -20)["pointer"] == [ox + 10, oy - 20]   # QA CR-4
    finally:
        launcher.stop(r["id"])


def test_runtime_limit_ends_the_engine_without_a_crash(engine_env, tmp_runtime):
    # QA SL-1, probed before the fix: RuntimeMaxSec took Xvfb down with the engine -> SIGABRT core.
    # Now the runner reaps the engine at its runtime limit and ends Xvfb after it.
    _, fso = engine_env
    mods = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=mods, pilot_mode="create",
                                      skip_movies=True, dismiss_popups=True, runtime_sec=25))
    try:
        end = time.monotonic() + 90
        while time.monotonic() < end and launcher.alive(session.load(r["id"])):
            time.sleep(1)
        rec = session.load(r["id"])
        assert rec.ended_by == "runtime_limit", rec.ended_by
        assert rec.exit_info and rec.exit_info["signal"] is None, rec.exit_info
    finally:
        launcher.stop(r["id"])

