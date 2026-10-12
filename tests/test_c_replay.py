"""Replay skips actions that did not run and never leaks the session it launched (QA SL-11, SL-9,
TA-8). launcher.launch / launcher.stop are stubbed: these are unit tests of replay's own logic."""
import os
import threading
import time
from pathlib import Path

import pytest

from fsoctl import channel, launcher, paths, record, session
from fsoctl.errors import EXIT_ENGINE, EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE, HarnessError
from tests.fake_bridge import FakeBridge


def _s():
    return session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=False, extra_args=[])


@pytest.fixture
def src(tmp_runtime, monkeypatch):
    """A source session with a journal, a stubbed launcher, and a log of what replay issued."""
    s = _s()
    rows = [("sexp", {"expr": "( a )"}, "ok"), ("sexp", {"expr": "( b )"}, "refused"),
            ("pilot", {"seconds": 1}, "timeout"), ("time", {"pause": True}, "ok")]
    for kind, args, outcome in rows:
        record.write(s, {**record.entry(s, kind, args), "state": None}, outcome)
    # A journal line from before outcomes existed: it was written before sending, so whether it
    # ran is unknown; replay keeps the old behaviour and re-issues it.
    record.write(s, {**record.entry(s, "keys", {"keys": ["a"]}), "state": None}, "ok")
    path = session.run_dir(s) / record.JOURNAL
    lines = path.read_text().splitlines()
    lines[-1] = lines[-1].replace(', "outcome": "ok"', "")
    path.write_text("\n".join(lines) + "\n")

    new = _s()
    stopped, issued = [], []
    monkeypatch.setattr(launcher, "launch", lambda req: {"id": new.id})
    monkeypatch.setattr(launcher, "stop", lambda sid: stopped.append(sid) or {"id": sid})
    monkeypatch.setattr(record, "_issue", lambda sid, kind, args: issued.append((sid, kind, args)))
    return {"src": s, "new": new, "stopped": stopped, "issued": issued}


def test_replay_skips_actions_that_did_not_run(src):
    r = record.replay(src["src"].id, speed=1000)
    assert [(k, a) for _, k, a in src["issued"]] == [
        ("sexp", {"expr": "( a )"}), ("time", {"pause": True}), ("keys", {"keys": ["a"]})]
    assert r["replayed"] == 3 and r["id"] == src["new"].id
    assert r["skipped"] == [{"index": 1, "kind": "sexp", "outcome": "refused"},
                            {"index": 2, "kind": "pilot", "outcome": "timeout"}]
    assert src["stopped"] == []          # a good replay leaves its session running


def test_replay_error_stops_the_new_session_and_names_it(src, monkeypatch):
    def refuse(sid, kind, args):
        raise HarnessError(EXIT_REFUSED, "no", "hint")
    monkeypatch.setattr(record, "_issue", refuse)
    with pytest.raises(HarnessError) as e:
        record.replay(src["src"].id, speed=1000)
    assert e.value.code == EXIT_REFUSED and e.value.details["id"] == src["new"].id
    assert e.value.message and e.value.hint
    assert src["stopped"] == [src["new"].id]


def test_replay_unexpected_error_stops_the_new_session_too(src, monkeypatch):
    def broken(sid, kind, args):
        raise KeyError("code")            # e.g. a hand-edited journal line missing an arg
    monkeypatch.setattr(record, "_issue", broken)
    with pytest.raises(HarnessError) as e:
        record.replay(src["src"].id, speed=1000)
    assert e.value.code == EXIT_USAGE and e.value.details["id"] == src["new"].id
    assert "KeyError" in e.value.message and e.value.hint
    assert src["stopped"] == [src["new"].id]


def test_replay_stop_failure_does_not_hide_the_original_error(src, monkeypatch):
    def refuse(sid, kind, args):
        raise HarnessError(EXIT_REFUSED, "no", "hint")

    def stop_fails(sid):
        raise HarnessError(EXIT_ENGINE, "stop failed", "x")
    monkeypatch.setattr(record, "_issue", refuse)
    monkeypatch.setattr(launcher, "stop", stop_fails)
    with pytest.raises(HarnessError) as e:
        record.replay(src["src"].id, speed=1000)
    assert e.value.code == EXIT_REFUSED and e.value.details["id"] == src["new"].id
    assert "stop failed" in e.value.hint


def test_replay_state_wait_timeout_quotes_heartbeat_age(tmp_runtime, monkeypatch):
    # QA SL-10: replay's "never reached" message used the last heartbeat as if it were current.
    s = _s()
    ch = channel.Channel(s)
    ch.dir.mkdir(parents=True)
    (ch.dir / "ready.json").write_text('{"state": "GS_STATE_BRIEFING"}')
    then = time.time() - 60
    os.utime(ch.dir / "ready.json", (then, then))
    monkeypatch.setattr(record, "_STATE_WAIT_SEC", 0.3)
    with pytest.raises(HarnessError) as e:
        record._wait_state(s, "GS_STATE_GAME_PLAY")
    assert e.value.code == EXIT_TIMEOUT and "GS_STATE_BRIEFING" in e.value.message
    assert "stale" in e.value.message and e.value.hint


# ---- X-1: a replayed shadow-folder session gets its own fresh shadow folder ------------------

def _shadow_source(tmp_path, cwd_name="abcd1234"):
    """A source session whose cwd is a shadow folder under the runtime root, like frame_stats'."""
    fs2 = tmp_path / "fs2"
    (fs2 / "data").mkdir(parents=True)
    (fs2 / "root.vp").write_bytes(b"VPVP")
    old = paths.runtime_root() / "shadows" / cwd_name
    old.mkdir(parents=True)
    s = session.new_session(kind="game", binary="/bin/true", fs2path=str(fs2), mods=[],
                            mission=None, renderer="opengl", width=1, height=1, switches={},
                            allow_eval=False, extra_args=["-profile_write_file"], cwd=str(old))
    return s, fs2, old


def test_replay_of_a_shadow_session_gets_a_fresh_shadow(tmp_runtime, tmp_path):
    # Review finding X-1: the rebuilt request dropped cwd, so -profile_write_file wrote into
    # FS2PATH.
    s, fs2, old = _shadow_source(tmp_path)
    req = record.launch_request_for(s)
    new = Path(req.cwd)
    assert new.parent == paths.runtime_root() / "shadows" and new != old
    assert (new / "data").resolve() == (fs2 / "data").resolve()      # a real shadow folder
    assert (new / "root.vp").is_symlink()
    assert req.extra_args == ["-profile_write_file"]


def test_replay_of_a_plain_session_keeps_no_cwd(tmp_runtime):
    req = record.launch_request_for(_s())
    assert req.cwd is None


def test_replay_never_reuses_a_missing_shadow(tmp_runtime, tmp_path):
    # The old folder may have been cleaned up; the replay still gets a working shadow.
    s, _, old = _shadow_source(tmp_path)
    old.rmdir()
    req = record.launch_request_for(s)
    assert req.cwd and Path(req.cwd).is_dir() and Path(req.cwd) != old


# ---- X-4: replay dispatches on a schedule, not after full recorded gaps -----------------------

@pytest.mark.parametrize(("speed", "want"), [(1.0, [0.0, 2.0, 5.0]), (2.0, [0.0, 2.0, 4.0])])
def test_replay_dispatches_at_scheduled_offsets(tmp_runtime, monkeypatch, speed, want):
    # Review finding X-4: sleeping the whole recorded gap AFTER a synchronous command counted
    # the
    # command's own run time twice. Fake clock: every command takes 2 s; recorded at t=0, 1, 5.
    s = _s()
    for t in (10.0, 11.0, 15.0):                       # offsets count from the first entry
        record.write(s, {"t": t, "kind": "keys", "args": {}, "state": None}, "ok")
    new = _s()
    clock = [100.0]
    sleeps, dispatched = [], []

    def fake_sleep(sec):
        sleeps.append(sec)
        clock[0] += sec

    def issue(sid, kind, args):
        dispatched.append(round(clock[0] - 100.0, 6))
        clock[0] += 2.0
    monkeypatch.setattr(launcher, "launch", lambda req: {"id": new.id})
    monkeypatch.setattr(launcher, "stop", lambda sid: {"id": sid})
    monkeypatch.setattr(record.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(record.time, "sleep", fake_sleep)
    monkeypatch.setattr(record, "_issue", issue)
    record.replay(s.id, speed=speed)
    assert dispatched == want
    assert all(x >= 0 for x in sleeps)                # late entries are not "slept" negatively


# ---- R6 / Y-5: replay order is issue order; uncertain timeouts are replayed and listed -------

def test_journal_keeps_issue_order_when_replies_finish_out_of_order(tmp_runtime, monkeypatch):
    # Y-5: a slow pilot is issued before a fast autopilot but finishes after it, so the journal
    # is appended autopilot-then-pilot. Replay must still issue pilot first.
    s = _s()
    ch = channel.Channel(s)
    with FakeBridge(ch.dir, script={"pilot": 0.6}):
        slow = threading.Thread(target=ch.send, args=("pilot", {"seconds": 1}))
        slow.start()
        time.sleep(0.2)
        ch.send("autopilot", {"on": True})
        slow.join(5)
    assert [e["kind"] for e in record.load(s.id)] == ["autopilot", "pilot"]   # append order
    new, issued = _s(), []
    monkeypatch.setattr(launcher, "launch", lambda req: {"id": new.id})
    monkeypatch.setattr(launcher, "stop", lambda sid: {"id": sid})
    monkeypatch.setattr(record, "_wait_state", lambda sess, want: None)
    monkeypatch.setattr(record, "_issue", lambda sid, kind, args: issued.append(kind))
    record.replay(s.id, speed=1000)
    assert issued == ["pilot", "autopilot"]


def test_replay_reissues_uncertain_timeouts_and_skips_withdrawn(tmp_runtime, monkeypatch):
    s = _s()
    rows = [("sexp", "ok"), ("pilot", "taken_unknown"), ("autopilot", "withdrawn"),
            ("time", "timeout")]                       # "timeout": a fix-wave-1 journal line
    for i, (kind, outcome) in enumerate(rows):
        record.write(s, {"t": float(i), "kind": kind, "args": {}, "state": None}, outcome)
    new, issued = _s(), []
    monkeypatch.setattr(launcher, "launch", lambda req: {"id": new.id})
    monkeypatch.setattr(record, "_issue", lambda sid, kind, args: issued.append(kind))
    monkeypatch.setattr(launcher, "stop", lambda sid: {"id": sid})
    r = record.replay(s.id, speed=1000)
    assert issued == ["sexp", "pilot"]
    assert r["uncertain"] == [{"index": 1, "kind": "pilot"}]
    assert r["skipped"] == [{"index": 2, "kind": "autopilot", "outcome": "withdrawn"},
                            {"index": 3, "kind": "time", "outcome": "timeout"}]


# ---- R3: cancellation (SIGTERM's SystemExit, Ctrl-C) stops the replay's session too ----------

@pytest.mark.parametrize("cancel", [SystemExit(143), KeyboardInterrupt()],
                         ids=["sigterm", "ctrl-c"])
def test_replay_cancelled_in_its_wait_stops_the_new_session(src, monkeypatch, cancel):
    # CR2-1 / SL2-1 / Y-2: `except Exception` let fsoctl's SIGTERM handler's SystemExit(143)
    # through without stopping the engine the replay had launched.
    def interrupted(want_sess, want):
        raise cancel
    monkeypatch.setattr(record, "_wait_state", interrupted)
    with pytest.raises(type(cancel)) as e:
        record.replay(src["src"].id, speed=1000)
    assert e.value is cancel                       # re-raised unchanged, exit code intact
    assert src["stopped"] == [src["new"].id]
