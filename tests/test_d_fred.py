"""Group D fix-wave tests for qtFRED verbs (QA SF-17, C9 staging, SL-9 / CR-5 cleanup)."""
import time
from pathlib import Path

import pytest

from fsoctl import fred, launcher, paths, session, xinput
from fsoctl.errors import EXIT_NOT_FOUND, EXIT_TIMEOUT, HarnessError

REAL_SLEEP = time.sleep


def _qtfred_session(mission: str):
    """A qtFRED session record whose mission is `mission`."""
    return session.new_session(kind="qtfred", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=mission, renderer="opengl", width=1280, height=720,
                               switches={}, allow_eval=False, extra_args=[])


# ---- C9: stage_mission ------------------------------------------------------------------------

def test_stage_mission_copies_into_fred_missions(tmp_path, tmp_runtime, monkeypatch):
    (tmp_path / "data" / "missions").mkdir(parents=True)
    (tmp_path / "data" / "missions" / "m.fs2").write_bytes(b"$Name: M\n")
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    copy = Path(fred.stage_mission("m.fs2", []))
    assert copy.is_absolute() and copy.name == "m.fs2"
    assert copy.is_relative_to(paths.runtime_root() / "fred-missions")
    assert copy.read_bytes() == b"$Name: M\n"
    assert Path(fred.stage_mission("m.fs2", [])) != copy   # every call gets its own copy


def test_stage_mission_missing_is_not_found(tmp_path, tmp_runtime, monkeypatch):
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    with pytest.raises(HarnessError) as e:
        fred.stage_mission("none.fs2", [])
    assert e.value.code == EXIT_NOT_FOUND


# ---- SL-9 / CR-5: open stops its session when anything after the launch fails -----------------

def test_open_stops_the_session_and_names_it_when_setup_fails(tmp_path, tmp_runtime,
                                                              monkeypatch):
    (tmp_path / "data" / "missions").mkdir(parents=True)
    (tmp_path / "data" / "missions" / "m.fs2").write_bytes(b"$Name: M\n")
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    s = _qtfred_session("/x/m.fs2")
    launched = []
    monkeypatch.setattr(launcher, "launch", lambda req: launched.append(req) or {"id": s.id})
    stopped: list[str] = []
    monkeypatch.setattr(launcher, "stop", stopped.append)

    def no_window(sess):
        raise HarnessError(EXIT_NOT_FOUND, "no main window for the session's process", "wait")
    monkeypatch.setattr(fred, "_window", no_window)
    with pytest.raises(HarnessError) as e:
        fred.open("m.fs2", binary="/bin/true")
    assert stopped == [s.id] and e.value.details["id"] == s.id
    assert Path(launched[0].mission).is_relative_to(paths.runtime_root() / "fred-missions")


# ---- SF-17: save waits for a stable size ------------------------------------------------------

@pytest.fixture
def saving(tmp_path, tmp_runtime, monkeypatch):
    """A qtFRED session whose 'qtFRED' writes the mission in pieces, one piece per sleep."""
    copy = tmp_path / "m.fs2"
    copy.write_bytes(b"old")
    s = _qtfred_session(str(copy))
    monkeypatch.setattr(launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(fred, "_focus", lambda sess: None)
    monkeypatch.setattr(fred, "_window", lambda sess: "0x1")
    monkeypatch.setattr(fred, "_visible_titles", lambda sess: ["m.fs2"])
    pieces: list[bytes] = []

    def fake_x(sess, *args):
        """Ctrl+S: qtFRED starts writing the file (first piece now, the rest on later sleeps)."""
        if args[-1] == "ctrl+s":
            REAL_SLEEP(0.01)   # so the new mtime differs from the old one
            copy.write_bytes(pieces.pop(0))
        return ""
    monkeypatch.setattr(xinput, "_x", fake_x)

    def fake_sleep(sec):
        """Each wait lets 'qtFRED' append its next piece."""
        if pieces:
            with open(copy, "ab") as f:
                f.write(pieces.pop(0))
        REAL_SLEEP(0.001)
    monkeypatch.setattr(fred.time, "sleep", fake_sleep)
    return s, copy, pieces


def test_save_returns_only_after_the_size_stops_changing(saving):
    s, copy, pieces = saving
    pieces += [b"$Name: part", b" two", b" three\n"]
    r = fred.save(s.id)
    assert r["saved"] is True and r["size"] == len(b"$Name: part two three\n")
    assert copy.read_bytes() == b"$Name: part two three\n"


def test_save_times_out_while_the_file_keeps_growing(saving, monkeypatch):
    s, _, pieces = saving
    pieces += [b"x"] * 100_000
    monkeypatch.setattr(fred, "_SAVE_TIMEOUT", 0.3)
    with pytest.raises(HarnessError) as e:
        fred.save(s.id)
    assert e.value.code == EXIT_TIMEOUT and "still" in e.value.message and e.value.hint


# ---- SF-17: fred_check and fred_selection caveats ---------------------------------------------

@pytest.mark.parametrize("count,n_entries,expected", [(1, 1, True), (2, 1, False),
                                                      (None, 1, False)])
def test_check_reports_whether_count_matches_entries(tmp_runtime, monkeypatch, count, n_entries,
                                                     expected):
    s = _qtfred_session("/x/m.fs2")
    monkeypatch.setattr(fred, "save", lambda sid: {"path": "/x/m.fs2", "saved": False,
                                                   "dialog": True})
    monkeypatch.setattr(fred, "_dialog_id", lambda sess: "0x2")
    monkeypatch.setattr(fred, "_close_dialog", lambda sess, did: None)
    monkeypatch.setattr(fred, "_focus", lambda sess: None)
    monkeypatch.setattr(xinput, "_x", lambda sess, *a: "")
    monkeypatch.setattr(fred.time, "sleep", lambda sec: None)   # retries (TA2-1) without waiting
    lines = [f"Error: entry {i}" for i in range(n_entries)]
    if count is not None:
        lines.append(f"{count} error(s) found.")
    monkeypatch.setattr(fred, "ocr", lambda path: "\n".join(lines))
    r = fred.check(s.id)
    assert r["count_matches_entries"] is expected


def test_clean_check_count_matches(monkeypatch):
    monkeypatch.setattr(fred, "save", lambda sid: {"path": "/x", "saved": True, "dialog": False})
    assert fred.check("s00000000")["count_matches_entries"] is True


def test_selection_says_it_is_ocr_of_a_fixed_crop(monkeypatch):
    monkeypatch.setattr(xinput, "screenshot", lambda sid, crop=None: {"path": "/x/shot.png"})
    monkeypatch.setattr(fred, "ocr", lambda path: "No Selection")
    r = fred.selection("s00000000")
    assert r["selection"] == "No Selection"
    assert "OCR" in r["note"] and fred._STATUS_LINE in r["note"]


# ---- fix wave 2, TA2-1: the dialog is read again until its count line and entries agree --------

CHECKER_OCR = (Path(__file__).parent / "data" / "checker_ocr.txt").read_text()


@pytest.fixture
def dialog(tmp_runtime, monkeypatch):
    """A qtFRED session whose save opened the error check; OCR answers from a list per capture."""
    s = _qtfred_session("/x/m.fs2")
    monkeypatch.setattr(fred, "save", lambda sid: {"path": "/x/m.fs2", "saved": False,
                                                   "dialog": True})
    monkeypatch.setattr(fred, "_dialog_id", lambda sess: "0x2")
    monkeypatch.setattr(fred, "_close_dialog", lambda sess, did: None)
    monkeypatch.setattr(fred, "_focus", lambda sess: None)
    captures: list[tuple] = []
    monkeypatch.setattr(xinput, "_x", lambda sess, *a: captures.append(a) or "")
    sleeps: list[float] = []
    monkeypatch.setattr(fred.time, "sleep", sleeps.append)
    readings: list[str] = []
    monkeypatch.setattr(fred, "ocr", lambda path: readings.pop(0))
    return s, readings, captures, sleeps


def test_check_reads_the_dialog_again_when_it_was_captured_unpainted(dialog):
    s, readings, captures, sleeps = dialog
    readings += ["", CHECKER_OCR]   # first capture: the window mapped but Qt had not painted it
    r = fred.check(s.id)
    assert r["count"] == 1 and r["count_matches_entries"] is True and r["attempts"] == 2
    assert len([c for c in captures if c[0] == "import"]) == 2 and sleeps == [0.4]


def test_check_gives_up_after_five_reads_and_says_so(dialog):
    s, readings, captures, sleeps = dialog
    readings += ["garbled"] * 5
    r = fred.check(s.id)
    assert r["attempts"] == 5 and r["count"] is None and r["count_matches_entries"] is False
    assert len([c for c in captures if c[0] == "import"]) == 5 and len(sleeps) == 4


@pytest.fixture
def selecting(monkeypatch):
    """Fake the drag, screenshot and OCR of fred.select; readings come from a list in order."""
    box = {"drags": 0, "readings": []}
    monkeypatch.setattr(xinput, "drag",
                        lambda sid, a, b: box.__setitem__("drags", box["drags"] + 1))
    monkeypatch.setattr(xinput, "screenshot", lambda sid, crop=None: {"path": "/x.png"})
    monkeypatch.setattr(fred, "ocr", lambda path: box["readings"].pop(0) if box["readings"]
                        else "No Selection")
    monkeypatch.setattr(fred.time, "sleep", lambda s: None)
    return box


def test_select_waits_for_the_status_line_to_change(selecting):
    # The qtFRED tier caught the status read before Qt repainted it (1 run in 3 read
    # "No Selection" right after a marquee that did select ships).
    selecting["readings"] = ["No Selection", "No Selection", "Alpha 1 (Fighter)"]
    r = fred.select("s1", (300, 120, 1250, 640))
    assert r["selection"] == "Alpha 1 (Fighter)" and selecting["drags"] == 1


def test_select_redoes_the_drag_once_when_nothing_changes(selecting):
    selecting["readings"] = ["No Selection"] * 12 + ["Iota 1"]
    r = fred.select("s1", (300, 120, 1250, 640))
    assert r["selection"] == "Iota 1" and selecting["drags"] == 2 and r["drags"] == 2


def test_select_reports_an_unchanged_status_after_the_retry(selecting):
    r = fred.select("s1", (300, 120, 1250, 640))
    assert r["selection"] == "No Selection" and r["changed"] is False and selecting["drags"] == 2
