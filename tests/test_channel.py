import multiprocessing
import os
import time

import pytest

from fsoctl import channel, session
from fsoctl.errors import EXIT_BRIDGE, EXIT_ENGINE, EXIT_REFUSED, EXIT_TIMEOUT, HarnessError
from tests.fake_bridge import FakeBridge


# spawn, not the platform default: Python 3.14's forkserver keeps the environment from when
# its server first started (an earlier test), so children would read another test's
# FSO_HARNESS_RUNTIME. spawn copies the current environment at each start.
_MP = multiprocessing.get_context("spawn")

def _sess():
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=False, extra_args=[])


def test_send_round_trip(tmp_runtime):
    s = _sess()
    ch = channel.Channel(s)
    with FakeBridge(ch.dir):
        assert ch.send("ping", {"a": 1}) == {"echo": {"a": 1}}
    assert not list(ch.dir.glob("res-*.json"))  # reply consumed


def test_refusal_becomes_harness_error(tmp_runtime):
    ch = channel.Channel(_sess())
    with FakeBridge(ch.dir), pytest.raises(HarnessError) as e:
        ch.send("refuse")
    assert e.value.code == EXIT_REFUSED and e.value.hint == "try x"


def test_timeout_names_heartbeat_state_and_withdraws_command(tmp_runtime):
    ch = channel.Channel(_sess())
    ch.dir.mkdir(parents=True)
    (ch.dir / "ready.json").write_text('{"state": "GS_STATE_BRIEFING", "frame": 3}')
    with pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=0.3, alive=lambda: True)
    assert e.value.code == EXIT_TIMEOUT and "GS_STATE_BRIEFING" in e.value.message
    assert not list(ch.dir.glob("cmd-*.json"))


def test_script_error_reply_is_bridge_error(tmp_runtime):
    # Contract C2 / QA SF-18: a Lua error is the harness breaking, not the caller's bad input.
    ch = channel.Channel(_sess())
    with FakeBridge(ch.dir), pytest.raises(HarnessError) as e:
        ch.send("boom")
    assert e.value.code == EXIT_BRIDGE and "nil value" in e.value.message and e.value.hint


def test_refusal_without_kind_stays_refused(tmp_runtime):
    # An older bridge sends no kind; C2 maps that to EXIT_REFUSED, as before.
    ch = channel.Channel(_sess())
    with FakeBridge(ch.dir), pytest.raises(HarnessError) as e:
        ch.send("refuse_nokind")
    assert e.value.code == EXIT_REFUSED and e.value.message == "nope" and e.value.hint == "try x"


def _old_heartbeat(ch, age):
    """Write a heartbeat whose file time is `age` seconds in the past."""
    ch.dir.mkdir(parents=True, exist_ok=True)
    hb = ch.dir / "ready.json"
    hb.write_text('{"state": "GS_STATE_BRIEFING", "frame": 3}')
    then = time.time() - age
    os.utime(hb, (then, then))


def test_heartbeat_reports_its_age(tmp_runtime):
    # Contract C3 / QA SL-10, SF-5: callers need to tell a live heartbeat from a hung engine's.
    ch = channel.Channel(_sess())
    _old_heartbeat(ch, 30)
    hb = ch.heartbeat()
    assert hb["state"] == "GS_STATE_BRIEFING" and 29 <= hb["age_sec"] <= 40


def test_timeout_quotes_heartbeat_age_and_says_stale(tmp_runtime):
    ch = channel.Channel(_sess())
    _old_heartbeat(ch, 30)
    with pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=0.3, alive=lambda: True)
    assert e.value.code == EXIT_TIMEOUT and "GS_STATE_BRIEFING" in e.value.message
    assert "stale" in e.value.message and "30" in e.value.message


def test_timeout_on_fresh_heartbeat_is_not_called_stale(tmp_runtime):
    ch = channel.Channel(_sess())
    _old_heartbeat(ch, 0)
    with pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=0.3, alive=lambda: True)
    assert "stale" not in e.value.message


def test_timeout_before_the_bridge_took_it_withdraws(tmp_runtime):
    # QA SL-6: no bridge read the file, so the command provably did not run.
    ch = channel.Channel(_sess())
    _old_heartbeat(ch, 0)
    with pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=0.3, alive=lambda: True)
    assert e.value.code == EXIT_TIMEOUT and "withdrawn; it did not run" in e.value.message
    assert e.value.hint
    assert not list(ch.dir.glob("cmd-*"))   # neither the command nor its .withdrawn copy is left


def test_timeout_after_the_bridge_took_it_says_outcome_unknown(tmp_runtime):
    # QA SL-6: the bridge deleted the command file (it is running, deferred), so "no reply" must
    # not read as "did not run"; retrying a non-idempotent command could run it twice.
    ch = channel.Channel(_sess())
    with FakeBridge(ch.dir, script={"ping": "hang"}), pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=0.3, alive=lambda: True)
    assert e.value.code == EXIT_TIMEOUT
    assert "the bridge took the command; outcome unknown" in e.value.message
    assert e.value.hint == "do not retry a non-idempotent command blindly; check state"


def test_dead_engine_is_reported_not_timed_out(tmp_runtime):
    ch = channel.Channel(_sess())
    ch.dir.mkdir(parents=True)
    with pytest.raises(HarnessError) as e:
        ch.send("ping", timeout=5, alive=lambda: False)
    assert e.value.code == EXIT_ENGINE and "crash_report" in e.value.hint


def _send_many(sid, who, n, out):
    """Child process: send n pings tagged with `who`; record (who echoed back, i echoed back)."""
    ch = channel.Channel(session.load(sid))
    for i in range(n):
        echo = ch.send("ping", {"i": i, "who": who})["echo"]
        out.append((echo["who"], echo["i"]))


def test_concurrent_callers_get_their_own_replies(tmp_runtime):
    # QA TA-12: distinct `who` per caller, so a reply delivered to the wrong caller is caught;
    # child exit codes are checked; children still running at the join deadline are killed.
    s = _sess()
    ch = channel.Channel(s)
    with FakeBridge(ch.dir) as fb, _MP.Manager() as m:
        outs = {"a": m.list(), "b": m.list()}
        procs = [_MP.Process(target=_send_many, args=(s.id, who, 20, o))
                 for who, o in outs.items()]
        for p in procs:
            p.start()
        hung = []
        for p in procs:
            p.join(30)
            if p.is_alive():
                hung.append(p.pid)
                p.terminate()
                p.join(5)
        assert hung == [] and [p.exitcode for p in procs] == [0, 0]
        for who, o in outs.items():
            assert list(o) == [(who, i) for i in range(20)]
        assert sorted(fb.seen) == list(range(1, 41))


# ---- QA TA-4: Channel.wait_ready's two failure branches, tested directly ----------------------

def test_wait_ready_reports_an_engine_that_died_before_the_bridge(tmp_runtime):
    ch = channel.Channel(_sess())
    t0 = time.monotonic()
    with pytest.raises(HarnessError) as e:
        ch.wait_ready(30, alive=lambda: False)
    assert e.value.code == EXIT_ENGINE and "before the bridge started" in e.value.message
    assert "crash_report" in e.value.hint and time.monotonic() - t0 < 5


def test_wait_ready_times_out_while_the_engine_lives(tmp_runtime):
    ch = channel.Channel(_sess())
    t0 = time.monotonic()
    with pytest.raises(HarnessError) as e:
        ch.wait_ready(0.6, alive=lambda: True)
    assert e.value.code == EXIT_TIMEOUT and "no bridge heartbeat" in e.value.message
    assert e.value.hint and 0.5 <= time.monotonic() - t0 < 5


def test_wait_ready_returns_the_first_heartbeat(tmp_runtime):
    ch = channel.Channel(_sess())
    _old_heartbeat(ch, 0)
    assert ch.wait_ready(5, alive=lambda: False)["state"] == "GS_STATE_BRIEFING"
