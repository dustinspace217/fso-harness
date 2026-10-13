import json

import pytest

from fsoctl import channel, record, session
from fsoctl.errors import EXIT_USAGE, HarnessError
from tests.fake_bridge import FakeBridge


def _s(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=["MVPS"], mission="m.fs2",
                renderer="opengl", width=800, height=600,
                switches={"skip_movies": True, "pilot_mode": "create", "pilot_name": "P"},
                allow_eval=False, extra_args=["-seed", "7"])
    base.update(kw)
    return session.new_session(**base)


def test_append_and_load_preserve_order_and_relative_time(tmp_runtime):
    s = _s()
    record.append(s, "keys", {"keys": ["Return"]})
    record.append(s, "sexp", {"expr": "( true )"})
    entries = record.load(s.id)
    assert [e["kind"] for e in entries] == ["keys", "sexp"]
    assert 0 <= entries[0]["t"] <= entries[1]["t"]


def test_channel_journals_actions_but_not_reads(tmp_runtime):
    s = _s()
    ch = channel.Channel(s)
    with FakeBridge(ch.dir):
        ch.send("ping")
        ch.send("state", {})
        ch.send("sexp", {"expr": "( true )"})
    assert [e["kind"] for e in record.load(s.id)] == ["sexp"]


def test_channel_journals_each_action_with_its_outcome(tmp_runtime):
    # QA SL-11: journal after the reply, so replay can tell what actually ran.
    s = _s()
    ch = channel.Channel(s)
    # Ruling R6 / SL2-5: a timeout is "taken_unknown" (the bridge took it) or "withdrawn" (it
    # provably did not run), never one undifferentiated "timeout".
    with FakeBridge(ch.dir, state="GS_STATE_GAME_PLAY",
                    script={"pilot": "hang", "autopilot": "ignore"}):
        ch.wait_ready(5, lambda: True)          # the fake's first heartbeat carries the state
        ch.send("sexp", {"expr": "( true )"})
        for cmd in ("refuse", "boom"):
            with pytest.raises(HarnessError):
                ch.send(cmd)
        with pytest.raises(HarnessError):
            ch.send("pilot", {"seconds": 1}, timeout=0.3)
        with pytest.raises(HarnessError):
            ch.send("autopilot", {"on": True}, timeout=0.3)
    with pytest.raises(HarnessError):
        ch.send("time", {"pause": True}, timeout=5, alive=lambda: False)
    entries = record.load(s.id)
    assert [(e["kind"], e["outcome"]) for e in entries] == [
        ("sexp", "ok"), ("refuse", "refused"), ("boom", "bridge_error"),
        ("pilot", "taken_unknown"), ("autopilot", "withdrawn"), ("time", "engine_dead")]
    assert entries[0]["state"] == "GS_STATE_GAME_PLAY"   # state when issued, not after
    seqs = [e["seq"] for e in entries]                    # the issue-order key (Y-5)
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_replay_plan_scales_offsets():
    entries = [{"t": 1.0, "kind": "keys", "args": {}}, {"t": 3.0, "kind": "sexp", "args": {}},
               {"t": 7.0, "kind": "keys", "args": {}}]
    assert record.offsets(entries, speed=2.0) == [0.0, 1.0, 3.0]
    assert record.offsets([], speed=2.0) == []


def test_bad_speed():
    with pytest.raises(HarnessError) as e:
        record.offsets([], speed=0)
    assert e.value.code == EXIT_USAGE


def test_launch_request_rebuilt_from_session(tmp_runtime):
    s = _s()
    req = record.launch_request_for(s)
    assert (req.mission, req.mods, req.width) == ("m.fs2", ["MVPS"], 800)
    assert req.extra_args == ["-seed", "7"]
    assert req.skip_movies and req.pilot_mode == "create" and req.pilot_name == "P"


def test_replay_note_is_honest():
    assert "not guaranteed" in record.REPLAY_NOTE


def test_journal_lines_are_json(tmp_runtime):
    s = _s()
    record.append(s, "click", {"x": 1, "y": 2})
    line = (session.run_dir(s) / "journal.jsonl").read_text().strip()
    assert json.loads(line)["args"] == {"x": 1, "y": 2}


def test_launch_request_carries_the_network_mode(tmp_runtime):
    """Replay keeps the opt-out: a real-network session replays on the real network (QA XFA-5);
    an old record (None) replays private, the current default."""
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                            renderer="opengl", width=1, height=1, switches={}, allow_eval=False,
                            extra_args=[], network="real")
    assert record.launch_request_for(s).network == "real"
    s2 = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                             renderer="opengl", width=1, height=1, switches={}, allow_eval=False,
                             extra_args=[])
    s2.network = None
    assert record.launch_request_for(s2).network == "private"
