import os
import time
from pathlib import Path

import pytest

from fsoctl import fred, launcher, paths, xinput

pytestmark = pytest.mark.qtfred
MISSION = os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2")
MODS = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]


@pytest.fixture
def fred_session(tmp_runtime):
    if not os.environ.get("QTFRED_BIN") or not os.environ.get("FS2PATH"):
        pytest.skip("QTFRED_BIN and FS2PATH required")
    r = fred.open(MISSION, mods=MODS)
    yield r
    launcher.stop(r["id"])


def test_camera_round_trip_on_a_copy(fred_session):
    vp_file = Path(os.environ["FS2PATH"]) / "root_fs2.vp"
    before = vp_file.stat().st_mtime_ns
    cam = fred.camera(fred_session["id"])
    # SM1-01's stored camera looks down and forward (fvec from the retail file, 2026-10-12).
    assert cam["fvec"] == pytest.approx([0.000151, -0.596933, -0.802291], abs=0.01)
    assert fred_session["mission_copy"].startswith(str(paths.runtime_root()))
    assert vp_file.stat().st_mtime_ns == before   # the user's game data is never written


def test_select_reports_status_text(fred_session):
    # The rectangle spans most of the 3D view of the 1280x720 editor, so it catches SM1-01's ships
    # whatever the exact layout. Before selecting, the status line reads "No Selection".
    assert "no selection" in fred.selection(fred_session["id"])["selection"].lower()
    sel = fred.select(fred_session["id"], (300, 120, 1250, 640))
    assert sel["selection"] and "no selection" not in sel["selection"].lower()


def test_full_screenshot_shows_a_dialog_and_the_window_origin(fred_session):
    # qtFRED dialogs are separate windows: a plain screenshot of the editor cannot show them
    # (SM1-01 demo finding 1). Shift+H opens Tools > Error Checker; the full shot must change.
    image = pytest.importorskip("PIL.Image")
    sid = fred_session["id"]
    before = xinput.screenshot(sid, full=True)
    sess = launcher.require_running(sid)
    # The whole display, not the editor (QA TA-5): runner._XVFB_EXTRA_HEIGHT more than the editor.
    assert (before["width"], before["height"]) == (sess.width, sess.height + 80)
    assert before["window_origin"] == xinput.window_origin(sess)
    xinput.keys(sid, ["shift+h"])
    time.sleep(1.5)                                  # let Qt map and paint the dialog
    after = xinput.screenshot(sid, full=True)
    chops = pytest.importorskip("PIL.ImageChops")
    stat = pytest.importorskip("PIL.ImageStat")
    diff = chops.difference(image.open(before["path"]).convert("L"),
                            image.open(after["path"]).convert("L"))
    assert stat.Stat(diff).mean[0] > 1.0


def test_stop_ends_qtfred_promptly_on_sigterm(tmp_runtime):
    # Before SDL_NO_SIGNAL_HANDLERS, qtFRED sat out the runner's 20 s grace and ended in SIGKILL.
    if not os.environ.get("QTFRED_BIN") or not os.environ.get("FS2PATH"):
        pytest.skip("QTFRED_BIN and FS2PATH required")
    r = fred.open(MISSION, mods=MODS)
    t0 = time.monotonic()
    out = launcher.stop(r["id"])
    assert time.monotonic() - t0 < 10
    assert out["exit_info"]["signal"] == "SIGTERM", out["exit_info"]
