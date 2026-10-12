import pytest

from fsoctl import game, launcher, record, session, xinput
from tests.engine_helpers import launch_in_mission

pytestmark = pytest.mark.engine


def test_replay_reissues_the_same_actions(engine_env, tmp_runtime):
    _, fso = engine_env
    src = launch_in_mission(fso, extra_args=["-seed", "7"])
    try:
        game.sexp(src, "( true )")
        game.pilot(src, pitch=0.5, seconds=0.5)
        xinput.keys(src, ["t"])
        game.state(src)   # read-only: not journaled
    finally:
        launcher.stop(src)
    kinds = [e["kind"] for e in record.load(src)]
    assert kinds == ["sexp", "pilot", "keys"]
    assert all(e["state"] == "GS_STATE_GAME_PLAY" for e in record.load(src))

    r = record.replay(src, speed=4.0)
    try:
        assert r["replayed"] == 3 and "not guaranteed" in r["note"]
        assert [e["kind"] for e in record.load(r["id"])] == kinds
        assert session.load(r["id"]).extra_args == ["-seed", "7"]
    finally:
        launcher.stop(r["id"])
