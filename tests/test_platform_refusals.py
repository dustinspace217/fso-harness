"""What Plan B does not do on Windows refuses by name (exit 7), before anything is created or
looked up: never a traceback, never a silent no-op (plan Review Focus 5)."""
import json

import pytest

from fsoctl import cli, fred, launcher, match, platform, xinput
from fsoctl.errors import EXIT_NOT_FOUND, EXIT_REFUSED, HarnessError
from fsoctl.launcher import LaunchRequest

NO_SESSION = "s00000000"        # does not exist: a lookup would say "no session", exit 3
NO_BINARY = "/no/such/engine"   # does not exist: a check of it would say so, exit 2

REFUSED = [
    ("--sound", "Plan E", lambda: launcher._validate(LaunchRequest(NO_BINARY, sound=True))),
    ("--network private", "Plan E",
     lambda: launcher._validate(LaunchRequest(NO_BINARY, network="private"))),
    ("--network sandbox", "Plan E",
     lambda: launcher._validate(LaunchRequest(NO_BINARY, network="sandbox"))),
    ("a match session", "Plan E",
     lambda: launcher._validate(LaunchRequest(NO_BINARY, match="m00000000", role="host"))),
    ("a match session", "Plan E",
     lambda: launcher._validate(LaunchRequest(NO_BINARY, match="m00000000"))),
    ("a match session", "Plan E",
     lambda: launcher._validate(LaunchRequest(NO_BINARY, role="host"))),
    ("match create", "Plan E", lambda: match.create("x")),
    ("an editor session (kind=qtfred)", "Plan C",
     lambda: launcher._validate(LaunchRequest(NO_BINARY, kind="qtfred"))),
    ("the keys verb", "Plan C", lambda: xinput.keys(NO_SESSION, ["a"])),
    ("the click verb", "Plan C", lambda: xinput.click(NO_SESSION, 1, 1)),
    ("the drag verb", "Plan C", lambda: xinput.drag(NO_SESSION, (1, 1), (2, 2))),
    ("the move verb", "Plan C", lambda: xinput.move(NO_SESSION, 1, 1)),
    ("the scroll verb", "Plan C", lambda: xinput.scroll(NO_SESSION, 1, 1, 1)),
    ("the fred_open verb", "Plan C", lambda: fred.open("sm1-01")),
    ("the fred_save verb", "Plan C", lambda: fred.save(NO_SESSION)),
    ("the fred_camera verb", "Plan C", lambda: fred.camera(NO_SESSION)),
    ("the fred_selection verb", "Plan C", lambda: fred.selection(NO_SESSION)),
    ("the fred_select verb", "Plan C", lambda: fred.select(NO_SESSION, (1, 1, 2, 2))),
    ("the fred_check verb", "Plan C", lambda: fred.check(NO_SESSION)),
]


@pytest.mark.parametrize("feature, plan, call", REFUSED, ids=[r[0] for r in REFUSED])
def test_each_refused_feature_refuses_on_windows(feature, plan, call, monkeypatch, tmp_runtime):
    monkeypatch.setattr(platform, "NAME", "windows")
    with pytest.raises(HarnessError) as e:
        call()
    assert e.value.code == EXIT_REFUSED
    assert e.value.message == f"{feature} is not available on windows"
    assert e.value.hint == f"tracked in {plan}"


@pytest.mark.parametrize("call", [r[2] for r in REFUSED if r[0].startswith("the ")
                                  and "fred_open" not in r[0]],
                         ids=[r[0] for r in REFUSED if r[0].startswith("the ")
                              and "fred_open" not in r[0]])
@pytest.mark.linux
def test_the_same_calls_are_not_refused_on_linux(call, tmp_runtime):
    # the control: on Linux these get as far as looking the session up
    with pytest.raises(HarnessError) as e:
        call()
    assert e.value.code == EXIT_NOT_FOUND


def test_a_refused_verb_exits_7_with_json_and_no_traceback(monkeypatch, tmp_runtime, capsys):
    monkeypatch.setattr(platform, "NAME", "windows")
    assert cli.main(["--json", "keys", NO_SESSION, "a"]) == 7
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": False, "code": 7, "message": "the keys verb is not available on windows",
                   "hint": "tracked in Plan C"}
    assert cli.main(["--json", "match", "create"]) == 7
    assert "match create is not available on windows" in capsys.readouterr().out


def test_launch_refuses_an_editor_before_it_looks_for_the_editors_program(monkeypatch,
                                                                         tmp_runtime, capsys):
    # QA Plan B, TA-10: with QTFRED_BIN unset the verb stopped at "export QTFRED_BIN"
    # (exit 2) and never reached the refusal
    monkeypatch.setattr(platform, "NAME", "windows")
    monkeypatch.delenv("QTFRED_BIN", raising=False)
    assert cli.main(["--json", "launch", "--qtfred"]) == 7
    assert "an editor session (kind=qtfred) is not available on windows" in capsys.readouterr().out


def test_launch_refuses_before_it_cleans_up_or_counts_sessions(monkeypatch, tmp_runtime):
    monkeypatch.setattr(platform, "NAME", "windows")

    def never(*arguments, **keywords):
        raise AssertionError("did work for a launch that is refused by name")
    monkeypatch.setattr(launcher, "_auto_cleanup", never)
    monkeypatch.setattr(launcher, "check_capacity", never)
    with pytest.raises(HarnessError) as e:
        launcher.launch(LaunchRequest(NO_BINARY, sound=True))
    assert e.value.code == EXIT_REFUSED and e.value.message == "--sound is not available on windows"
