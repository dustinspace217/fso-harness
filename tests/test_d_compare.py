"""Group D fix-wave tests for compare's verdict (QA SF-12, TA-9)."""
import pytest

from fsoctl import compare, session


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
