import os
from pathlib import Path

import pytest

from fsoctl import fred, launcher, paths

pytestmark = pytest.mark.qtfred
MODS = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]


@pytest.fixture
def need_qtfred(tmp_runtime):
    if not os.environ.get("QTFRED_BIN") or not os.environ.get("FS2PATH"):
        pytest.skip("QTFRED_BIN and FS2PATH required")


def _broken_sm1_01(tmp_path: Path) -> Path:
    """SM1-01 with event "Iota 1 Dock" pointed at a ship that does not exist (as in the probe)."""
    data, _ = fred.find_mission("SM1-01.fs2", MODS, paths.fs2path())
    text = data.decode("latin-1")
    events = text.index("#Events")
    first = text.index('"Iota 1"', events)
    broken = text[:first] + '"Ghost 9"' + text[first + len('"Iota 1"'):]
    out = tmp_path / "SM1-01.fs2"
    out.write_bytes(broken.encode("latin-1"))
    return out


def test_broken_mission_is_reported(need_qtfred, tmp_path):
    r = fred.open(str(_broken_sm1_01(tmp_path)), mods=MODS)
    try:
        c = fred.check(r["id"])
        assert c["clean"] is False and c["count"] == 1
        assert any(e["bad_node"] == "Ghost 9" for e in c["entries"])
        assert Path(c["screenshot"]).exists()
        assert fred.check(r["id"])["clean"] is False   # dialog was closed, check runs again
    finally:
        launcher.stop(r["id"])


def test_clean_mission_saves(need_qtfred):
    r = fred.open("SM1-01.fs2", mods=MODS)
    try:
        assert fred.check(r["id"])["clean"] is True
    finally:
        launcher.stop(r["id"])
