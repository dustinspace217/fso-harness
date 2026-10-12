import os

import pytest

from fsoctl import launcher, xinput
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine
# Pillow is a test-only dependency (python3-pillow); without it this module is skipped, so tools
# that run the unit tier in their own environment (mutmut) still collect cleanly.
Image = pytest.importorskip("PIL.Image")


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
