from pathlib import Path

import pytest

from fsoctl import fred
from fsoctl.errors import EXIT_NOT_FOUND, HarnessError
from tests.test_vp import write_vp

# The camera block of retail SM1-01, bytes as stored (CRLF line ends).
SNIPPET = (Path(__file__).parent / "data" / "viewer_mission_snippet.fs2").read_bytes().decode(
    "latin-1")


def test_parse_viewer():
    v = fred.parse_viewer(SNIPPET)
    assert v["pos"] == pytest.approx([-2759.583008, 11636.9375, 12197.359375])
    assert v["fvec"] == pytest.approx([0.000151, -0.596933, -0.802291])
    assert len(v["orient"]) == 3 and all(len(row) == 3 for row in v["orient"])


def test_parse_viewer_missing_block():
    with pytest.raises(HarnessError):
        fred.parse_viewer("$Name: nothing here")


def test_find_mission_prefers_first_mod_loose_file(tmp_path):
    (tmp_path / "ModA" / "data" / "missions").mkdir(parents=True)
    (tmp_path / "ModA" / "data" / "missions" / "m.fs2").write_bytes(b"from mod")
    (tmp_path / "data" / "missions").mkdir(parents=True)
    (tmp_path / "data" / "missions" / "m.fs2").write_bytes(b"from root")
    data, source = fred.find_mission("m.fs2", ["ModA"], tmp_path)
    assert data == b"from mod" and "ModA" in source


def test_find_mission_falls_back_to_vp_archives(tmp_path):
    write_vp(tmp_path / "root_fs2.vp", {"data/missions/SM1-01.fs2": b"retail"})
    data, source = fred.find_mission("sm1-01.fs2", [], tmp_path)
    assert data == b"retail" and source.endswith("root_fs2.vp")


def test_find_mission_absolute_path(tmp_path):
    p = tmp_path / "x.fs2"
    p.write_bytes(b"abs")
    assert fred.find_mission(str(p), [], tmp_path) == (b"abs", str(p))


def test_find_mission_missing(tmp_path):
    with pytest.raises(HarnessError) as e:
        fred.find_mission("none.fs2", [], tmp_path)
    assert e.value.code == EXIT_NOT_FOUND
