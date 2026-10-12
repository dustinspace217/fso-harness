"""Group D fix-wave tests for model_info's completeness report (QA SF-9)."""
from pathlib import Path

import pytest

from fsoctl import launcher, model_info, session

POFSPEW = (Path(__file__).parent / "data" / "pofspew_sample.txt").read_text()


@pytest.fixture
def spew(tmp_runtime, monkeypatch):
    """Fake the -pofspew run: write the real sample into a session, with a chosen exit_info."""
    def run(exit_info):
        s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                                mission=None, renderer="opengl", width=1, height=1,
                                switches={}, allow_eval=False, extra_args=[])
        out = session.prefs_dir(s) / "fso_harness_bridge" / "data"
        out.mkdir(parents=True)
        (out / "pofspew.txt").write_text(POFSPEW)
        s.status, s.exit_info = "exited", exit_info
        session.save(s)
        monkeypatch.setattr(launcher, "run_to_exit", lambda req, timeout: session.load(s.id))
        return model_info.model_info([], binary="/bin/true")
    return run


def test_clean_exit_is_complete(spew):
    r = spew({"returncode": 0, "signal": None, "at": 1.0})
    assert r["complete"] is True and r["exit_info"]["returncode"] == 0
    assert r["count"] == 3 and "hint" not in r


@pytest.mark.parametrize("exit_info", [
    {"returncode": -11, "signal": "SIGSEGV", "at": 1.0},    # crashed part-way through
    {"returncode": 1, "signal": None, "at": 1.0},           # non-zero exit
    None,                                                     # exit never recorded
])
def test_unclean_exit_is_incomplete_and_points_at_crash_report(spew, exit_info):
    r = spew(exit_info)
    assert r["complete"] is False and r["exit_info"] == exit_info
    assert "crash_report" in r["hint"]
    assert r["count"] == 3   # the partial list is still returned, labelled incomplete
