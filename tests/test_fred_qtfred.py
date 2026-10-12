import os
from pathlib import Path

import pytest

from fsoctl import fred, launcher, paths

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
