import threading
import time

import pytest

from fsoctl import game, logs, session
from fsoctl.errors import EXIT_ENGINE, EXIT_TIMEOUT, EXIT_USAGE, HarnessError


def _sess():
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=False, extra_args=[])


@pytest.fixture
def live(tmp_runtime, monkeypatch):
    s = _sess()
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    return s


def test_bad_kind():
    with pytest.raises(HarnessError) as e:
        game.check_wait_kind("sometime")
    assert e.value.code == EXIT_USAGE


def test_bad_log_regex_is_usage_error(live):
    with pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "log", "(", timeout=1)
    assert e.value.code == EXIT_USAGE


def test_log_wait_sees_new_line(live):
    log = logs.sources(live)["engine"]
    log.write_text("Mission loaded: old\n")

    def later():
        time.sleep(0.5)
        with open(log, "a") as f:
            f.write("Mission loaded: sm1-01\n")
    t = threading.Thread(target=later)
    t.start()
    r = game.wait_for(live.id, "log", r"Mission loaded: sm1", timeout=5)
    t.join(5)
    assert r["met"] and r["after_sec"] >= 0.4


def test_log_wait_ignores_lines_before_the_call(live):
    logs.sources(live)["engine"].write_text("Mission loaded: sm1-01\n")
    with pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "log", r"Mission loaded", timeout=0.6)
    assert e.value.code == EXIT_TIMEOUT


def test_wait_stops_when_engine_dies(live, monkeypatch):
    logs.sources(live)["engine"].write_text("")
    monkeypatch.setattr(game.launcher, "alive", lambda sess: False)
    t0 = time.monotonic()
    with pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "log", r"never", timeout=30)
    assert e.value.code == EXIT_ENGINE and time.monotonic() - t0 < 5
