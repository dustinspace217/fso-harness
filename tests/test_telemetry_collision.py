"""Unit tests for the Python side of telemetry and collision alerts (game.py).

The bridge side (recording, the collision filter, pilot's stop) is tested in tests/lua/run.lua;
these pin argument checking, what reaches the bridge, and the collision wait's baseline.
"""
import pytest

from fsoctl import cli, game, session
from fsoctl.errors import EXIT_BRIDGE, EXIT_TIMEOUT, EXIT_USAGE, HarnessError


@pytest.fixture
def live(tmp_runtime, monkeypatch):
    """A running game session whose bridge calls are recorded and answered by `answers`.

    `sent` gets (cmd, args); `calls` gets (sid, cmd, timeout) so tests can pin which session and
    which timeout each call used (mutation pass MU-3).
    """
    s = session.new_session(kind="game", binary="/bin/true", fs2path="/tmp", mods=[],
                            mission=None, renderer="opengl", width=1, height=1, switches={},
                            allow_eval=False, extra_args=[])
    monkeypatch.setattr(game.launcher, "require_running", lambda sid: s)
    monkeypatch.setattr(game.launcher, "alive", lambda sess: True)
    sent, answers, calls = [], [], []

    def fake_call(sid, cmd, args=None, timeout=None):
        sent.append((cmd, args))
        calls.append((sid, cmd, timeout))
        return answers.pop(0) if answers else {"ok": True}

    monkeypatch.setattr(game, "call", fake_call)
    return s, sent, answers, calls


@pytest.mark.parametrize("action", ["on", "off", "read", "clear"])
def test_telemetry_sends_its_action(live, action):
    s, sent, _, calls = live
    game.telemetry(s.id, action, seconds=30, hz=4)
    assert sent == [("telemetry", {"action": action, "seconds": 30, "hz": 4})]
    assert calls[0][0] == s.id                                  # to this session


def test_telemetry_defaults(live):
    s, sent, _, _ = live
    game.telemetry(s.id, "read")
    assert sent == [("telemetry", {"action": "read", "seconds": 10.0, "hz": 2.0})]


def test_telemetry_refusal_messages(live):
    s, _, _, _ = live
    with pytest.raises(HarnessError) as e:
        game.telemetry(s.id, "start")
    assert e.value.message == "bad telemetry action 'start'"
    assert e.value.hint == "use one of on, off, read, clear"
    with pytest.raises(HarnessError) as e:
        game.telemetry(s.id, "read", seconds=500)
    assert e.value.message == "telemetry seconds 500 / hz 2.0 out of range"
    assert e.value.hint == "seconds 0.1-120, hz 0.1-10"


@pytest.mark.parametrize("kwargs", [dict(action="start"), dict(action="read", seconds=121),
                                    dict(action="read", seconds=0.05), dict(action="read", hz=11),
                                    dict(action="read", hz=0.05)])
def test_telemetry_refuses_bad_arguments_before_the_bridge(live, kwargs):
    s, sent, _, _ = live
    with pytest.raises(HarnessError) as e:
        game.telemetry(s.id, **kwargs)
    assert e.value.code == EXIT_USAGE and sent == []


@pytest.mark.parametrize("seconds, hz", [(0.1, 0.1), (120, 10)])
def test_telemetry_accepts_the_range_ends(live, seconds, hz):
    s, sent, _, _ = live
    game.telemetry(s.id, "read", seconds=seconds, hz=hz)
    assert len(sent) == 1


def test_collision_wait_baselines_then_waits_past_it(live):
    s, sent, answers, calls = live
    answers += [{"met": True, "detail": {"total": 3, "last": None}},          # baseline
                {"met": False, "detail": {"total": 3, "last": None}},
                {"met": True, "detail": {"total": 4, "last": {"other": "Iota"}}}]
    r = game.wait_for(s.id, "collision", "any", timeout=5, poll=0.01)
    assert r["met"] is True and r["detail"]["last"]["other"] == "Iota"
    assert sent[0] == ("check", {"kind": "collision", "value": -1})
    assert sent[1] == ("check", {"kind": "collision", "value": 3})          # compared to baseline
    assert sent[2] == ("check", {"kind": "collision", "value": 3})
    assert calls[0] == (s.id, "check", 10)                           # baseline call


@pytest.mark.parametrize("reply", [{"met": False, "detail": "not in a mission"},
                                   {"met": True}, {"met": True, "detail": {"total": "3"}},
                                   {"met": True, "detail": {"total": True}}])
def test_collision_wait_refuses_a_reply_without_a_count(live, reply):
    s, _, answers, _ = live
    answers.append(reply)
    with pytest.raises(HarnessError) as e:
        game.wait_for(s.id, "collision", "any", timeout=1, poll=0.01)
    assert e.value.code == EXIT_BRIDGE
    assert e.value.message.startswith("the bridge reported no collision count: {")
    assert e.value.hint == ("the loaded bridge may predate collision alerts; "
                            "stop and launch again")


def test_collision_wait_timeout_names_a_new_contact(live):
    s, _, answers, _ = live
    answers += [{"met": True, "detail": {"total": 3}}] + [{"met": False, "detail": {}}] * 400
    with pytest.raises(HarnessError) as e:
        game.wait_for(s.id, "collision", "any", timeout=0.05, poll=0.01)
    assert e.value.code == EXIT_TIMEOUT and "collision a new contact not met" in e.value.message


@pytest.mark.parametrize("flag", ["false", 0, None])
def test_pilot_refuses_a_non_bool_stop_on_collision(live, flag):
    s, sent, _, _ = live
    with pytest.raises(HarnessError) as e:
        game.pilot(s.id, seconds=1, stop_on_collision=flag)
    assert e.value.code == EXIT_USAGE and sent == []
    assert e.value.message == "stop_on_collision must be true or false"
    assert e.value.hint == f"got {flag!r}"


def test_pilot_sends_stop_on_collision(live):
    s, sent, _, calls = live
    game.pilot(s.id, forward=1.0, seconds=2)
    assert calls[0][0] == s.id
    game.pilot(s.id, forward=1.0, seconds=2, stop_on_collision=False)
    assert sent[0][1]["stop_on_collision"] is True
    assert sent[1][1]["stop_on_collision"] is False


def test_cli_flag_reaches_pilot(live):
    s, sent, _, _ = live
    parser = cli.build_parser()
    ns = parser.parse_args(["pilot", s.id, "--no-stop-on-collision"])
    cli.VERBS["pilot"][2](ns)
    assert sent[-1][1]["stop_on_collision"] is False
    ns = parser.parse_args(["pilot", s.id])
    cli.VERBS["pilot"][2](ns)
    assert sent[-1][1]["stop_on_collision"] is True                          # the default
    ns = parser.parse_args(["telemetry", s.id, "read", "--seconds", "5", "--hz", "1"])
    cli.VERBS["telemetry"][2](ns)
    assert sent[-1] == ("telemetry", {"action": "read", "seconds": 5.0, "hz": 1.0})
