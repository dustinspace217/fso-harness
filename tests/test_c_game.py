"""Group C fix-wave tests for game.py: wait_for injection refusal (SA-1), qtFRED refusal (CR-15),
byte-offset log waits (SF-6 / CR-3), and pilot_cancel on a pilot timeout (SL-5)."""
import json
import threading
import time

import pytest

from fsoctl import channel, game, logs, record, session
from fsoctl.errors import EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE, HarnessError
from tests.fake_bridge import FakeBridge

# The SA-1 probe's payload (probes/SA-1-result.md): it ran Lua with no --allow-eval.
SA1_PAYLOAD = 'x" ( script-eval-num "ba.print([[FSOH_PWN_MARKER]]) return 0" ) "y'


def _sess(allow=False, kind="game"):
    return session.new_session(kind=kind, binary="/bin/true", fs2path="/tmp", mods=[],
                               mission=None, renderer="opengl", width=1, height=1, switches={},
                               allow_eval=allow, extra_args=[])


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    """Keep contract C7's persistent audit log out of the user's real state folder."""
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "state"))


@pytest.fixture
def live(tmp_runtime, monkeypatch):
    """A 'running' game session without eval, as require_running would return it."""
    s = _sess()
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    return s


# ---- SA-1: wait_for values that the bridge pastes into sexp text ------------------------------

@pytest.mark.parametrize("kind", ["event_true", "event_false", "destroyed", "departed"])
def test_sa1_payload_never_reaches_the_bridge(live, kind):
    with FakeBridge(channel.Channel(live).dir) as fb, pytest.raises(HarnessError) as e:
        game.wait_for(live.id, kind, SA1_PAYLOAD, timeout=2)
    assert e.value.code == EXIT_REFUSED and e.value.message and e.value.hint
    assert fb.cmds == []                       # refused before any command file was written
    assert not (session.run_dir(live) / "audit.log").exists()


@pytest.mark.parametrize("value", ['a"b', "a(b", "a)b", "a!b", "a\nb", "a\x00b", "a\x7fb"])
def test_sexp_delimiters_in_names_are_refused(live, value):
    with FakeBridge(channel.Channel(live).dir) as fb, pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "event_true", value, timeout=2)
    assert e.value.code == EXIT_REFUSED and fb.cmds == []


def test_script_shaped_name_needs_eval(live):
    # No delimiter, but contains "eval": the same gate as the sexp verb.
    with FakeBridge(channel.Channel(live).dir) as fb, pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "destroyed", "script-eval-num", timeout=2)
    assert e.value.code == EXIT_REFUSED and "allow-eval" in e.value.hint and fb.cmds == []


def test_script_shaped_name_with_eval_is_audited(tmp_runtime, monkeypatch):
    s = _sess(allow=True)
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    with FakeBridge(channel.Channel(s).dir), pytest.raises(HarnessError):
        game.wait_for(s.id, "destroyed", "Evaluator 1", timeout=0.5)   # fake never says "met"
    entry = json.loads((session.run_dir(s) / "audit.log").read_text().splitlines()[0])
    assert entry["kind"] == "wait_for-script" and entry["text"] == "destroyed Evaluator 1"


def test_plain_names_still_reach_the_bridge(live):
    with FakeBridge(channel.Channel(live).dir) as fb, pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "event_true", "Destroy Iota's escort", timeout=0.5)
    assert e.value.code == EXIT_TIMEOUT and "check" in fb.cmds   # passed the filter


# ---- CR-15: game verbs on a qtFRED session ------------------------------------------------------

@pytest.mark.parametrize("verb", [
    game.state,
    lambda sid: game.call(sid, "ping"),
    lambda sid: game.wait_for(sid, "state", "GS_STATE_GAME_PLAY", timeout=30),
    game.answer_popup,
])
def test_game_verbs_refuse_a_qtfred_session_at_once(tmp_runtime, monkeypatch, verb):
    s = _sess(kind="qtfred")
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    t0 = time.monotonic()
    with pytest.raises(HarnessError) as e:
        verb(s.id)
    assert e.value.code == EXIT_REFUSED and "qtfred" in e.value.message and e.value.hint
    assert time.monotonic() - t0 < 1          # not a 10 s wait for a bridge that is not there


# ---- SF-6 / CR-3: wait_for log reads bytes and only whole lines -------------------------------

def _append_later(path, *chunks, gap=0.6):
    """Append each chunk to `path` after `gap` seconds, in a thread; return the thread."""
    def run():
        for c in chunks:
            time.sleep(gap)
            with open(path, "ab") as f:
                f.write(c)
    t = threading.Thread(target=run)
    t.start()
    return t


def test_log_wait_matches_a_line_finished_after_a_poll(live):
    log = logs.sources(live)["engine"]
    log.write_bytes(b"")
    t = _append_later(log, b"Mission lo", b"aded\n")    # several polls see the half line
    r = game.wait_for(live.id, "log", r"Mission loaded", timeout=5)
    t.join(5)
    assert r["met"] and r["detail"] == "Mission loaded"


def test_log_wait_matches_a_line_half_written_before_the_call(live):
    # Block-buffered stdout can stop mid-line; the line's end arrives after the call.
    log = logs.sources(live)["engine"]
    log.write_bytes(b"old line\nMission lo")
    t = _append_later(log, b"aded\n")
    r = game.wait_for(live.id, "log", r"Mission loaded", timeout=5)
    t.join(5)
    assert r["met"] and r["detail"] == "Mission loaded"


class _ReadSpy:
    """Wraps an open binary file and records how many bytes each read() returned."""

    def __init__(self, f, sizes):
        self.f, self.sizes = f, sizes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()

    def read(self, n=-1):
        b = self.f.read(n)
        self.sizes.append(len(b))
        return b

    def __getattr__(self, name):
        return getattr(self.f, name)


def test_log_wait_reads_only_new_bytes(live, monkeypatch):
    log = logs.sources(live)["engine"]
    log.write_bytes(b"x" * 100_000 + b"\n")
    sizes = []
    real_open = open

    def spy_open(path, mode="r", *a, **kw):
        f = real_open(path, mode, *a, **kw)
        return _ReadSpy(f, sizes) if str(path) == str(log) else f
    monkeypatch.setattr("builtins.open", spy_open)
    with pytest.raises(HarnessError):
        game.wait_for(live.id, "log", r"never", timeout=0.8)
    assert len(sizes) > 2 and max(sizes) <= 4096   # several polls; never the 100 kB already there


# ---- SL-5 / ruling R5: a pilot that times out is cancelled by its own seq ---------------------

def _no_replies_left(live):
    """True when no reply file is left in the session's channel folder."""
    return not list(channel.Channel(live).dir.glob("res-*.json"))


def test_taken_pilot_timeout_cancels_that_flight_and_reaps_its_reply(live, monkeypatch):
    monkeypatch.setattr(game, "_pilot_timeout", lambda seconds: 0.3)
    with FakeBridge(channel.Channel(live).dir, script={"pilot": "hang"}) as fb, \
            pytest.raises(HarnessError) as e:
        game.pilot(live.id, forward=1.0, seconds=1)
        # (raises; the fake answered the cancelled pilot late, as the real bridge does)
    pilot_seq = fb.seen[0]
    assert e.value.code == EXIT_TIMEOUT and fb.calls[1] == ("pilot_cancel", {"seq": pilot_seq})
    assert "cancelled" in e.value.message and e.value.details["pilot_cancel"] == {"cancelled": True}
    assert _no_replies_left(live)             # the late pilot reply was reaped (SL2-8 / Y-12)


def test_withdrawn_pilot_timeout_sends_no_cancel(live, monkeypatch):
    # The bridge never took the pilot, so it never flew; a cancel could only hit someone else's
    # flight (Y-4).
    monkeypatch.setattr(game, "_pilot_timeout", lambda seconds: 0.3)
    with FakeBridge(channel.Channel(live).dir, script={"pilot": "ignore"}) as fb, \
            pytest.raises(HarnessError) as e:
        game.pilot(live.id, forward=1.0, seconds=1)
    assert e.value.code == EXIT_TIMEOUT and "withdrawn" in e.value.message
    assert fb.cmds == [] and "pilot_cancel" not in e.value.details


def test_cancel_for_a_flight_that_already_ended_reports_no_match(live, monkeypatch):
    # The pilot finished just after its caller gave up: the cancel names it, finds no matching
    # flight, and the pilot's own late reply is still reaped.
    monkeypatch.setattr(game, "_pilot_timeout", lambda seconds: 0.3)
    with FakeBridge(channel.Channel(live).dir, script={"pilot": 0.5}), \
            pytest.raises(HarnessError) as e:
        game.pilot(live.id, forward=1.0, seconds=1)
    assert e.value.details["pilot_cancel"]["cancelled"] is False
    assert "no matching flight" in e.value.message
    assert _no_replies_left(live)


def test_pilot_timeout_with_failed_cancel_says_flight_may_still_run(live, monkeypatch):
    monkeypatch.setattr(game, "_pilot_timeout", lambda seconds: 0.3)
    with FakeBridge(channel.Channel(live).dir,
                    script={"pilot": "hang", "pilot_cancel": "fail"}), \
            pytest.raises(HarnessError) as e:
        game.pilot(live.id, forward=1.0, seconds=1)
    assert e.value.code == EXIT_TIMEOUT and "may still be running" in e.value.message
    assert "pilot_cancel" in e.value.hint


def test_pilot_success_sends_no_cancel(live):
    with FakeBridge(channel.Channel(live).dir) as fb:
        game.pilot(live.id, forward=1.0, seconds=1)
    assert fb.cmds == ["pilot"]


def test_pilot_cancel_is_not_journaled(live, monkeypatch):
    # Ruling R6: replaying a cancel would cancel nothing (or the wrong flight).
    monkeypatch.setattr(game, "_pilot_timeout", lambda seconds: 0.3)
    with FakeBridge(channel.Channel(live).dir, script={"pilot": "hang"}), \
            pytest.raises(HarnessError):
        game.pilot(live.id, forward=1.0, seconds=1)
    assert [e["kind"] for e in record.load(live.id)] == ["pilot"]


# ---- length caps: the engine copies a sexp into char buf[8192] (sexp.cpp run_sexp); the bridge
# refuses 8000 bytes or more, and wait_for names are capped lower because the bridge wraps them.

def test_sexp_just_under_the_cap_reaches_the_bridge(live):
    with FakeBridge(channel.Channel(live).dir) as fb:
        game.sexp(live.id, "x" * 7999)
    assert fb.cmds == ["sexp"]


@pytest.mark.parametrize("expr", ["x" * 8000, "\u00e9" * 4000],   # 8000 UTF-8 bytes each
                         ids=["ascii", "two-byte"])
def test_sexp_at_the_cap_is_usage_error(live, expr):
    with FakeBridge(channel.Channel(live).dir) as fb, pytest.raises(HarnessError) as e:
        game.sexp(live.id, expr)
    assert e.value.code == EXIT_USAGE and "8000" in e.value.message and e.value.hint
    assert fb.cmds == []


def test_wait_name_just_under_the_cap_reaches_the_bridge(live):
    with FakeBridge(channel.Channel(live).dir) as fb, pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "destroyed", "a" * 1023, timeout=0.3)
    assert e.value.code == EXIT_TIMEOUT and "check" in fb.cmds


@pytest.mark.parametrize("kind", ["event_true", "event_false", "destroyed", "departed"])
def test_wait_name_at_the_cap_is_usage_error(live, kind):
    with FakeBridge(channel.Channel(live).dir) as fb, pytest.raises(HarnessError) as e:
        game.wait_for(live.id, kind, "a" * 1024, timeout=2)
    assert e.value.code == EXIT_USAGE and "1024" in e.value.message and fb.cmds == []


# ---- caller text echoed in messages is clipped to 200 characters --------------------------------

def test_wait_timeout_clips_the_echoed_value(live):
    with FakeBridge(channel.Channel(live).dir), pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "destroyed", "a" * 1000, timeout=0.3)
    assert "a" * 200 + "\u2026" in e.value.message and "a" * 201 not in e.value.message


def test_refused_name_is_clipped_in_the_message(live):
    with pytest.raises(HarnessError) as e:
        game.wait_for(live.id, "destroyed", "a" * 500 + "(", timeout=2)
    assert e.value.code == EXIT_REFUSED and "a" * 201 not in e.value.message


def test_bad_wait_kind_is_clipped_in_the_message():
    with pytest.raises(HarnessError) as e:
        game.check_wait_kind("k" * 500)
    assert "k" * 200 + "\u2026" in e.value.message and "k" * 201 not in e.value.message
