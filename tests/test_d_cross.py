"""Group D tests for the cross-family review addendum (X-2, X-3, X-5, X-6, X-7, XK-3..5, XK-9)."""
import os
import struct

import pytest

from fsoctl import build, docs, fred, launcher, scenario, scope, session, vp, xinput
from fsoctl.errors import EXIT_FAILED, EXIT_NOT_FOUND, EXIT_USAGE, HarnessError
from tests.test_vp import write_vp


# ---- XK-3: the directory offset must lie inside the file ---------------------------------------

@pytest.mark.parametrize("diroff", [-16, 4, 10_000])
def test_vp_directory_offset_outside_the_file_is_refused(tmp_path, diroff):
    arc = tmp_path / "t.vp"
    with open(arc, "wb") as f:
        f.write(struct.pack("<4siii", b"VPVP", 2, diroff, 0))
        f.write(b"abcd")
    with pytest.raises(HarnessError) as e:
        vp.read_index(arc)
    assert e.value.code == EXIT_USAGE and "directory" in e.value.message


# ---- X-3: a failed build says so in the exit code ----------------------------------------------

def test_failed_build_carries_exit_failed(tmp_path, tmp_runtime, monkeypatch):
    wt = tmp_path / "wt"
    (wt / "build").mkdir(parents=True)
    (wt / "build" / "CMakeCache.txt").write_text("")
    monkeypatch.setattr(scope, "caps", lambda **kw: [])
    monkeypatch.setattr(scope, "is_active", lambda unit: False)

    def run(cmd, stdout=None, **kw):
        stdout.write(b"/w/a.cpp:1:1: error: nope\n")
        return build.subprocess.CompletedProcess(cmd, 1)
    monkeypatch.setattr(build.subprocess, "run", run)
    r = build.build(str(wt))
    assert r["ok"] is False and r["code"] == EXIT_FAILED


# ---- XK-4: scenario validation -----------------------------------------------------------------

@pytest.mark.parametrize("steps,needs", [
    ([{"wait_for": {}}], "kind"),
    ([{"wait_for": {"kind": "state"}}], "value"),
    ([{"sexp": {"mode": "eval"}}], "expr"),
])
def test_scenario_steps_need_their_keys(steps, needs):
    with pytest.raises(HarnessError) as e:
        scenario.validate(steps)
    assert e.value.code == EXIT_USAGE and needs in e.value.message and "step 0" in e.value.message


def test_duplicate_snapshot_labels_are_refused():
    with pytest.raises(HarnessError) as e:
        scenario.validate([{"snapshot": "t"}, {"sleep": 0}, {"snapshot": "t"}])
    assert e.value.code == EXIT_USAGE and "step 2" in e.value.message and "'t'" in e.value.message


# ---- X-2 / XK-9: mission lookup follows the engine's root order --------------------------------

def _mission(root, data: bytes) -> None:
    (root / "data" / "missions").mkdir(parents=True, exist_ok=True)
    (root / "data" / "missions" / "m.fs2").write_bytes(data)


def test_first_mods_archive_beats_a_later_mods_loose_file(tmp_path):
    # cfilesystem.cpp: each mod's folder, then that mod's archives, before the next mod.
    (tmp_path / "ModA").mkdir()
    write_vp(tmp_path / "ModA" / "a.vp", {"data/missions/m.fs2": b"A archive"})
    _mission(tmp_path / "ModB", b"B loose")
    data, source = fred.find_mission("m.fs2", ["ModA", "ModB"], tmp_path)
    assert data == b"A archive" and source.endswith("a.vp")


def test_within_one_root_loose_beats_archive(tmp_path):
    _mission(tmp_path / "ModA", b"A loose")
    write_vp(tmp_path / "ModA" / "a.vp", {"data/missions/m.fs2": b"A archive"})
    assert fred.find_mission("m.fs2", ["ModA"], tmp_path)[0] == b"A loose"


def test_archives_are_searched_in_case_insensitive_name_order(tmp_path):
    # cf_packfile_sort_func: stricmp on the path, and *.[vV][pP] matches either case.
    write_vp(tmp_path / "b.vp", {"data/missions/m.fs2": b"b"})
    write_vp(tmp_path / "A.VP", {"data/missions/m.fs2": b"A"})
    assert fred.find_mission("m.fs2", [], tmp_path)[0] == b"A"


def test_a_corrupt_archive_is_skipped_and_reported(tmp_path):
    (tmp_path / "a_broken.vp").write_bytes(b"not an archive at all")
    write_vp(tmp_path / "b.vp", {"data/missions/m.fs2": b"found"})
    skipped: list[dict] = []
    data, _ = fred.find_mission("m.fs2", [], tmp_path, skipped)
    assert data == b"found"
    assert [s["archive"] for s in skipped] == [str(tmp_path / "a_broken.vp")]
    assert skipped[0]["error"]


def test_not_found_names_the_skipped_archives(tmp_path):
    (tmp_path / "a_broken.vp").write_bytes(b"junk")
    with pytest.raises(HarnessError) as e:
        fred.find_mission("m.fs2", [], tmp_path)
    assert e.value.code == EXIT_NOT_FOUND and "a_broken.vp" in e.value.message


def test_open_reports_skipped_archives(tmp_path, tmp_runtime, monkeypatch):
    (tmp_path / "a_broken.vp").write_bytes(b"junk")
    write_vp(tmp_path / "b.vp", {"data/missions/m.fs2": b"found"})
    monkeypatch.setenv("FS2PATH", str(tmp_path))
    s = session.new_session(kind="qtfred", binary="/bin/true", fs2path=str(tmp_path), mods=[],
                            mission="/x", renderer="opengl", width=1, height=1, switches={},
                            allow_eval=False, extra_args=[])
    monkeypatch.setattr(launcher, "launch", lambda req: {"id": s.id})
    monkeypatch.setattr(fred, "_window", lambda sess: "0x1")
    monkeypatch.setattr(xinput, "_x", lambda sess, *a: "")
    monkeypatch.setattr(fred, "_LAYOUT_SETTLE", 0)
    r = fred.open("m.fs2", binary="/bin/true")
    assert [x["archive"] for x in r["skipped_archives"]] == [str(tmp_path / "a_broken.vp")]


# ---- X-5: the docs cache key covers FS2PATH and what the mods contain --------------------------

@pytest.fixture
def fs2(tmp_path, monkeypatch):
    root = tmp_path / "fs2"
    (root / "ModA" / "data" / "tables").mkdir(parents=True)
    (root / "ModA" / "data" / "tables" / "x-sexp.tbm").write_text("#Lua SEXPs\n")
    monkeypatch.setenv("FS2PATH", str(root))
    exe = tmp_path / "fs2_open"
    exe.write_bytes(b"engine")
    return root, str(exe)


def test_docs_key_changes_with_fs2path(fs2, tmp_path, monkeypatch):
    _, exe = fs2
    first = docs.cache_dir(exe, ["ModA"])
    other = tmp_path / "other"
    (other / "ModA").mkdir(parents=True)
    monkeypatch.setenv("FS2PATH", str(other))
    assert docs.cache_dir(exe, ["ModA"]) != first


@pytest.mark.parametrize("change", ["table size", "new script", "vp", "table mtime"])
def test_docs_key_changes_with_mod_contents(fs2, change):
    root, exe = fs2
    first = docs.cache_dir(exe, ["ModA"])
    tbm = root / "ModA" / "data" / "tables" / "x-sexp.tbm"
    if change == "table size":
        tbm.write_text("#Lua SEXPs\n$Operator: new-op\n")
    elif change == "new script":
        (root / "ModA" / "data" / "scripts").mkdir()
        (root / "ModA" / "data" / "scripts" / "y-sct.lua").write_text("--")
    elif change == "vp":
        (root / "ModA" / "pack.vp").write_bytes(b"VPVP")
    else:
        st = tbm.stat()
        os.utime(tbm, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert docs.cache_dir(exe, ["ModA"]) != first


def test_docs_key_ignores_files_outside_the_fingerprint(fs2):
    root, exe = fs2
    first = docs.cache_dir(exe, ["ModA"])
    (root / "ModA" / "data" / "missions").mkdir(parents=True)
    (root / "ModA" / "data" / "missions" / "m.fs2").write_text("x")
    assert docs.cache_dir(exe, ["ModA"]) == first


# ---- X-6 / XK-5 / X-7: input is bounded and never left held ------------------------------------

@pytest.fixture
def xcalls(tmp_runtime, monkeypatch):
    """Record every X tool call; require_running / window_id faked."""
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=1, height=1, switches={},
                            allow_eval=False, extra_args=[])
    calls: list[tuple] = []
    monkeypatch.setattr(launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(xinput, "window_id", lambda sess: "0x1")
    monkeypatch.setattr(xinput, "_x", lambda sess, *a: calls.append(a) or "")
    monkeypatch.setattr(xinput.time, "sleep", lambda sec: None)
    return s, calls


@pytest.mark.parametrize("hold", [-1, 10_001])
def test_hold_ms_out_of_range_is_refused_before_any_input(xcalls, hold):
    s, calls = xcalls
    with pytest.raises(HarnessError) as e:
        xinput.keys(s.id, ["w"], hold_ms=hold)
    assert e.value.code == EXIT_USAGE and calls == []


def test_hold_ms_at_the_bounds_is_accepted(xcalls):
    s, calls = xcalls
    xinput.keys(s.id, ["w"], hold_ms=10_000)
    xinput.keys(s.id, ["w"], hold_ms=0)
    assert ("xdotool", "keyup", "w") in calls and ("xdotool", "key", "w") in calls


def test_a_failed_hold_still_releases_the_key(xcalls, monkeypatch):
    s, calls = xcalls

    def boom(sec):
        raise KeyboardInterrupt
    monkeypatch.setattr(xinput.time, "sleep", boom)
    with pytest.raises(KeyboardInterrupt):
        xinput.keys(s.id, ["w"], hold_ms=500)
    assert calls[-1] == ("xdotool", "keyup", "w")


def test_a_failed_drag_still_releases_the_button(xcalls, monkeypatch):
    s, calls = xcalls
    moves = []

    def move(sess, x, y):
        moves.append((x, y))
        if len(moves) == 2:   # the window vanished mid-drag
            raise HarnessError(EXIT_NOT_FOUND, "xdotool failed", "is the session running?")
    monkeypatch.setattr(xinput, "_move", move)
    with pytest.raises(HarnessError):
        xinput.drag(s.id, (0, 0), (10, 10))
    assert calls[-1] == ("xdotool", "mouseup", "1")


def test_a_failed_release_after_a_failure_keeps_the_first_error(xcalls, monkeypatch):
    s, calls = xcalls

    def boom(sec):
        raise KeyboardInterrupt

    def x(sess, *a):
        calls.append(a)
        if a[1] == "keyup":
            raise HarnessError(EXIT_NOT_FOUND, "xdotool failed", "gone")
        return ""
    monkeypatch.setattr(xinput.time, "sleep", boom)
    monkeypatch.setattr(xinput, "_x", x)
    with pytest.raises(KeyboardInterrupt):
        xinput.keys(s.id, ["w"], hold_ms=500)


@pytest.mark.parametrize("clicks", [51, -51])
def test_scroll_over_the_cap_is_refused(xcalls, clicks):
    s, calls = xcalls
    with pytest.raises(HarnessError) as e:
        xinput.scroll(s.id, 5, 5, clicks)
    assert e.value.code == EXIT_USAGE and "50" in e.value.message and calls == []


def test_scroll_at_the_cap_sends_every_click(xcalls):
    s, calls = xcalls
    assert xinput.scroll(s.id, 5, 5, -50) == {"scrolled": -50}
    assert calls.count(("xdotool", "click", "5")) == 50
