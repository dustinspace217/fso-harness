import pytest

from fsoctl import compare, scenario
from fsoctl.errors import EXIT_USAGE, HarnessError


@pytest.mark.parametrize("steps,where", [
    ([{"fly": {}}], "step 0"),
    ([{"sleep": 1}, {"sleep": 121}], "step 1"),
    ([{"sleep": 1, "snapshot": "x"}], "step 0"),
    ([{"snapshot": ""}], "step 0"),
    ("not a list", "list"),
])
def test_scenario_validation_names_the_bad_step(steps, where):
    with pytest.raises(HarnessError) as e:
        scenario.validate(steps)
    assert e.value.code == EXIT_USAGE and where in e.value.message


def test_scenario_validation_accepts_every_step_kind():
    steps = [{"wait_for": {"kind": "mission_time", "value": 5, "timeout": 60}},
             {"sexp": {"expr": "( true )"}}, {"pilot": {"pitch": 0.5, "seconds": 1}},
             {"autopilot": True}, {"time": {"compression": 200}}, {"keys": ["t"]},
             {"sleep": 0.5}, {"snapshot": "t5"}]
    assert scenario.validate(steps) == steps


def _state(**ships):
    return {"state": "GS_STATE_GAME_PLAY",
            "ships": [{"name": n, "pos": p, "hull_pct": h} for n, (p, h) in ships.items()],
            "goals": [{"name": "G", "status": "incomplete"}],
            "events": [{"name": "E", "status": "pending"}]}


def test_diff_states_tolerances_and_changes():
    a = _state(A=([0, 0, 0], 100.0), B=([0, 0, 0], 100.0), C=([0, 0, 0], 100.0))
    b = _state(A=([0.5, 0, 0], 100.0), B=([50, 0, 0], 90.0))
    b["events"][0]["status"] = "true"
    diffs = compare.diff_states(a, b, pos_tol=1.0, hull_tol=1.0)
    kinds = sorted((d["kind"], d.get("name")) for d in diffs)
    assert kinds == [("event", "E"), ("hull", "B"), ("missing_in_b", "C"), ("position", "B")]
    pos = next(d for d in diffs if d["kind"] == "position")
    assert pos["delta"] == pytest.approx(50.0)


def test_identical_states_have_no_diffs():
    a = _state(A=([1, 2, 3], 80.0))
    assert compare.diff_states(a, a) == []


def test_parse_mae_output():
    assert compare.parse_mae("123.4 (0.00188)") == pytest.approx(0.00188)
    with pytest.raises(HarnessError):
        compare.parse_mae("garbage")
