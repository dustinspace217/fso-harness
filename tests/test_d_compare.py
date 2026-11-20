"""Group D fix-wave tests for compare's verdict (QA SF-12, TA-9)."""
import pytest

from fsoctl import compare, session
from fsoctl.errors import EXIT_LAUNCH, HarnessError


def _state(goals=("G",), events=("E",), truncated=False):
    """A state snapshot with one ship and the named goals/events."""
    st = {"state": "GS_STATE_GAME_PLAY",
          "ships": [{"name": "Alpha 1", "pos": [0, 0, 0], "hull_pct": 100.0}],
          "goals": [{"name": g, "status": "incomplete"} for g in goals],
          "events": [{"name": e, "status": "pending"} for e in events]}
    st[compare.SHIPS_TRUNCATED_KEY] = truncated
    return st


def _run(sid, **snaps):
    """A finished run as _run_once returns it: {label: {"state", "screenshot"}}."""
    return {"id": sid, "renderer": "opengl",
            "snapshots": {label: {"state": st, "screenshot": f"/x/{sid}-{label}.png"}
                          for label, st in snaps.items()}}


@pytest.fixture
def fake_runs(tmp_runtime, monkeypatch):
    """Patch the two engine runs and the image diff; return a setter for runs and image error."""
    sb = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                             mission=None, renderer="opengl", width=1, height=1, switches={},
                             allow_eval=False, extra_args=[])
    box = {"runs": [], "mae": 0.0}

    def run_once(binary, launch, steps, renderer):
        return box["runs"].pop(0)
    monkeypatch.setattr(compare, "_run_once", run_once)
    monkeypatch.setattr(compare.programs, "find", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(compare, "diff_images",
                        lambda a, b, out: {"mean_abs_diff": box["mae"], "diff_image": out})

    def set_runs(a_snaps: dict, b_snaps: dict, mae: float = 0.0):
        box["runs"] = [_run("sa000000", **a_snaps), _run(sb.id, **b_snaps)]
        box["mae"] = mae
    return set_runs


def _compare():
    return compare.compare("/a", "/b", {}, [{"snapshot": "t"}])


def test_identical_runs_are_identical(fake_runs):
    fake_runs({"t": _state()}, {"t": _state()})
    r = _compare()
    assert r["identical_within_tolerance"] is True and r["states_identical"] is True
    assert r["not_identical_because"] == []


def test_no_snapshots_is_not_identical(fake_runs):
    fake_runs({}, {})
    r = _compare()
    assert r["identical_within_tolerance"] is False and r["states_identical"] is False
    assert any("no snapshot" in x for x in r["not_identical_because"])


def test_mismatched_snapshot_labels_are_not_identical(fake_runs):
    fake_runs({"t": _state(), "u": _state()}, {"t": _state()})
    r = _compare()
    assert r["identical_within_tolerance"] is False
    assert r["only_in_a"] == ["u"] and r["only_in_b"] == []
    assert r["states_identical"] is False
    assert any(x.startswith("snapshot labels differ") for x in r["not_identical_because"])


@pytest.mark.parametrize("key", ["goals", "events"])
def test_goal_or_event_in_one_run_only_is_a_difference(fake_runs, key):
    # Explicit keywords (not **{key: ...}) so a type checker sees no tuple reaching `truncated`.
    a = _state(goals=("X", "Y")) if key == "goals" else _state(events=("X", "Y"))
    b = _state(goals=("X",)) if key == "goals" else _state(events=("X",))
    fake_runs({"t": a}, {"t": b})
    r = _compare()
    assert r["identical_within_tolerance"] is False and r["states_identical"] is False
    kind = "goal" if key == "goals" else "event"
    assert {"kind": "only_in_a", "of": kind, "name": "Y"} in r["snapshots"]["t"]["state_diffs"]
    assert "t: 1 state difference(s)" in r["not_identical_because"]


def test_truncated_ship_list_is_not_identical(fake_runs):
    fake_runs({"t": _state(truncated=True)}, {"t": _state(truncated=True)})
    r = _compare()
    assert r["identical_within_tolerance"] is False and r["states_identical"] is False
    assert any("truncated" in x for x in r["not_identical_because"])


def test_image_difference_over_tolerance_is_not_identical(fake_runs):
    fake_runs({"t": _state()}, {"t": _state()}, mae=0.02)
    r = _compare()
    assert r["identical_within_tolerance"] is False and r["states_identical"] is True
    assert any("image" in x for x in r["not_identical_because"])


def test_image_difference_within_tolerance_is_identical(fake_runs):
    fake_runs({"t": _state()}, {"t": _state()}, mae=0.01)   # the default tolerance, inclusive
    assert _compare()["identical_within_tolerance"] is True


def test_unreadable_hull_or_position_does_not_crash_the_diff():
    # The bridge reports unreadable fields as null (QA SF-15); a null vs a number is a difference.
    a = _state()
    b = _state()
    b["ships"][0]["hull_pct"] = None
    b["ships"][0]["pos"] = None
    kinds = sorted(d["kind"] for d in compare.diff_states(a, b))
    assert kinds == ["hull", "position"]
    assert compare.diff_states(b, b) == []


def test_ship_list_without_a_truncation_flag_is_not_certified(fake_runs):
    # An older bridge (before SF-15) sends no ships_truncated: unknown, so never "identical".
    a, b = _state(), _state()
    del a[compare.SHIPS_TRUNCATED_KEY]
    fake_runs({"t": a}, {"t": b})
    r = _compare()
    assert r["identical_within_tolerance"] is False and r["states_identical"] is False
    assert r["snapshots"]["t"]["ships_truncated"] is None
    assert any("unknown" in x for x in r["not_identical_because"])


def test_a_machine_without_imagemagick_is_refused_before_any_session_starts(fake_runs,
                                                                           monkeypatch):
    # Seen on Windows (2026-11-17), where ImageMagick is not part of the system: both
    # sessions ran, and only then the picture comparison failed with a bare
    # FileNotFoundError. The check now comes first, on every OS.
    monkeypatch.setattr(compare.programs, "find", lambda name: None)
    started = []
    monkeypatch.setattr(compare, "_run_once", lambda *a: started.append(a))
    for call in (_compare, lambda: compare.renderer_compare("/a", {}, [{"snapshot": "t"}])):
        with pytest.raises(HarnessError) as e:
            call()
        assert e.value.code == EXIT_LAUNCH and "ImageMagick" in e.value.message
        assert "magick" in e.value.hint
    assert started == []


def test_with_imagemagick_present_the_runs_start(fake_runs, monkeypatch):
    monkeypatch.setattr(compare.programs, "find", lambda name: f"/usr/bin/{name}")
    fake_runs({"t": {}}, {"t": {}})
    assert _compare()["a"] == "sa000000"


def test_the_program_looked_for_is_magick(fake_runs, monkeypatch):
    # QA Plan B, MU-3: any other name on PATH is not ImageMagick
    monkeypatch.setattr(compare.programs, "find",
                        lambda name: "/usr/bin/magick" if name == "magick" else None)
    compare._need_magick()
    monkeypatch.setattr(compare.programs, "find",
                        lambda name: None if name == "magick" else f"/usr/bin/{name}")
    with pytest.raises(HarnessError):
        compare._need_magick()


def test_on_windows_imagemagick_is_started_by_the_full_path_it_was_found_at(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return compare.subprocess.CompletedProcess(argv, 0, "", "0 (0)")
    monkeypatch.setattr(compare.subprocess, "run", run)
    monkeypatch.setattr(compare.programs, "find", lambda name: rf"C:\tools\{name}.exe")
    monkeypatch.setattr(compare.platform, "NAME", "windows")
    compare.diff_images("a.png", "b.png", "d.png")
    assert seen[0][0] == r"C:\tools\magick.exe"
    monkeypatch.setattr(compare.platform, "NAME", "linux")
    compare.diff_images("a.png", "b.png", "d.png")
    assert seen[1][0] == "magick"                       # the plain name, as before


def test_runs_on_different_displays_are_not_identical():
    # QA CR-1: Weston (GPU) and Xvfb (software) pixels differ for the same build, so a pair of
    # runs that landed on different displays must not read as a build difference or a match.
    a = {**_run("sa000000", t=_state()), "display_server": "weston"}
    b = {**_run("sb000000", t=_state()), "display_server": "xvfb"}
    r = compare._verdict(a, b, {}, compare._DEFAULT_TOL)
    assert any("different displays (a: weston, b: xvfb)" in x for x in r["not_identical_because"])
    same = compare._verdict(a, {**b, "display_server": "weston"}, {}, compare._DEFAULT_TOL)
    assert not any("different displays" in x for x in same["not_identical_because"])


def test_compare_runs_default_to_xvfb(monkeypatch):
    seen = []
    monkeypatch.setattr(compare.launcher, "launch",
                        lambda req: seen.append(req.display) or {"id": "s1", "renderer": "opengl",
                                                                 "display_server": "xvfb"})
    monkeypatch.setattr(compare.scenario, "run", lambda sid, steps: {"t": "snap"})
    monkeypatch.setattr(compare.launcher, "stop", lambda sid: None)
    assert compare._run_once("/a", {}, [], "opengl") == {
        "id": "s1", "renderer": "opengl", "display_server": "xvfb", "snapshots": {"t": "snap"}}
    compare._run_once("/a", {"display": "auto"}, [], "opengl")
    monkeypatch.setattr(compare.platform, "NAME", "windows")     # QA XFA-1/XFK-1
    compare._run_once("/a", {}, [], "opengl")
    assert seen == ["xvfb", "auto", "auto"]


def test_renderer_compare_reports_different_displays(fake_runs):
    # QA XFA-6: renderer_compare never read the displays.
    fake_runs({"t": _state()}, {"t": _state()})
    r = compare.renderer_compare("/a", {}, [{"snapshot": "t"}])
    assert r["displays"] == [None, None] and "warnings" not in r



def test_renderer_compare_warns_when_the_displays_differ(tmp_runtime, monkeypatch):
    sb = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                             mission=None, renderer="vulkan", width=1, height=1, switches={},
                             allow_eval=False, extra_args=[])
    runs = [{**_run("sa000000", t=_state()), "display_server": "xvfb"},
            {**_run(sb.id, t=_state()), "display_server": "weston"}]
    calls = []
    monkeypatch.setattr(compare, "_run_once", lambda *a: calls.append(a) or runs.pop(0))
    monkeypatch.setattr(compare.programs, "find", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(compare, "diff_images", lambda a, b, out: {"mean_abs_diff": 0.0,
                                                                   "diff_image": out})
    steps = [{"snapshot": "t"}]
    r = compare.renderer_compare("/a", {"mods": "M"}, steps)
    assert r["warnings"] == ["the runs used different displays (opengl: xvfb, vulkan: weston); "
                             "give both the same --display"]
    assert calls == [("/a", {"mods": "M"}, steps, "opengl"), ("/a", {"mods": "M"}, steps, "vulkan")]
    assert (r["opengl"], r["vulkan"], r["renderers"]) == ("sa000000", sb.id, ["opengl", "opengl"])
    assert r["displays"] == ["xvfb", "weston"] and set(r["snapshots"]) == {"t"}
