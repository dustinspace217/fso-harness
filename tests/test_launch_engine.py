import os

import pytest

from fsoctl import launcher, session
from fsoctl.launcher import LaunchRequest

pytestmark = pytest.mark.engine


def test_launch_heartbeat_status_stop(engine_env, tmp_runtime):
    _, fso = engine_env
    req = LaunchRequest(binary=str(fso), mods=[os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")],
                        skip_movies=True)
    r = launcher.launch(req)
    try:
        assert r["renderer"] == "opengl"
        st = launcher.status(r["id"])
        assert st["status"] == "running" and st["alive"]
        assert st["heartbeat"]["state"].startswith("GS_STATE_")
        info = launcher.bridge_info(r["id"])
        assert info["switches"]["skip_movies"] is True
    finally:
        out = launcher.stop(r["id"])
    assert out["status"] in ("stopped", "exited")
    assert launcher.stop(r["id"])["status"] in ("stopped", "exited")  # idempotent (Review Focus 4)
    assert not launcher.alive(session.load(r["id"]))
