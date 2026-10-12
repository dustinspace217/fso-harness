"""Unit tier for tests/engine_helpers.py: launch_in_mission must not leak a session.

QA findings TA2-6 / Y-7: when gameplay never arrives (or the run is interrupted), the engine
fixtures never reach the `launcher.stop` after their yield, so the helper itself must stop it.
"""
import pytest

from tests import engine_helpers


def test_launch_in_mission_stops_on_failure(monkeypatch):
    stopped = []

    def boom(*_a, **_k):
        raise SystemExit(143)   # what the C8 SIGTERM handler raises

    monkeypatch.setattr(engine_helpers.launcher, "launch", lambda _req: {"id": "s00000001"})
    monkeypatch.setattr(engine_helpers.launcher, "stop", stopped.append)
    monkeypatch.setattr(engine_helpers, "wait_hb", boom)
    with pytest.raises(SystemExit):
        engine_helpers.launch_in_mission("/bin/true")
    assert stopped == ["s00000001"]
