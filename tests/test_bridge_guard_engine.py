"""Engine tier: the bridge's own guards on caller-built sexps (QA finding SA-1, also SF-2/CR-12).

These tests talk to the bridge through game.call, which has no Python-side eval check, so they
prove the BRIDGE refuses on its own. The injected Lua would create a file in the session's
channel folder through the engine's cf API; that file is visible on release and debug builds
alike (ba.print is compiled out of release builds, so a log marker would prove nothing there).
"""
import pytest

from fsoctl import channel, game, launcher, paths, session
from fsoctl.errors import EXIT_REFUSED, HarnessError
from tests.engine_helpers import launch_in_mission

pytestmark = pytest.mark.engine

MARKER = "fsoh-sa1-marker.txt"
# Lua that leaves MARKER in data/cache (the channel folder) if it ever runs. script-eval-num
# compiles "return <text>", so the payload must be one expression (a called function literal).
_LUA = (f"(function() local f = cf.openFile([[{MARKER}]], [[w]], [[data/cache]]) "
        "f:write([[x]]) f:close() return 0 end)()")
# The SA-1 probe's shape: close the name's string early, splice in script-eval-num, reopen it.
# Sexp string tokens hold at most 31 characters (TOKEN_LENGTH 32, globals.h) and script-eval-num
# concatenates all its arguments (sexp.cpp sexp_script_eval), so the Lua goes in 30-char pieces.
_PIECES = " ".join(f'"{_LUA[i:i + 30]}"' for i in range(0, len(_LUA), 30))
INJECT = f'x" ( script-eval-num {_PIECES} ) "y'


@pytest.fixture
def mission(engine_env, tmp_runtime):
    _, fso = engine_env
    sid = launch_in_mission(fso)
    yield sid
    launcher.stop(sid)


def _refused(sid: str, args: dict) -> HarnessError:
    """Send one `check` straight to the bridge and return the refusal it must produce."""
    with pytest.raises(HarnessError) as e:
        game.call(sid, "check", args)
    return e.value


def test_injected_names_are_refused_and_never_run(mission):
    for kind in ("event_true", "event_false", "destroyed", "departed"):
        err = _refused(mission, {"kind": kind, "value": INJECT})
        assert err.code == EXIT_REFUSED, (kind, err.message)
    chan = channel.Channel(session.load(mission)).dir
    assert not (chan / MARKER).exists()          # the Lua never ran
    assert launcher.alive(session.load(mission))  # and the engine survived the attempt


def test_unknown_names_are_refused_not_pending(mission):
    # SF-2: a misspelled event or ship used to read as "pending" / "not destroyed" forever.
    assert "no event named" in _refused(mission, {"kind": "event_true",
                                                  "value": "No Such Event"}).message
    assert "no ship named" in _refused(mission, {"kind": "destroyed",
                                                 "value": "No Such Ship"}).message
    # The real ones still answer.
    assert game.call(mission, "check", {"kind": "destroyed", "value": "Alpha 1"})["met"] is False
    # An expression the engine cannot parse answers false like a real false (probed 2026-10-12),
    # so the reply carries a note saying so.
    r = game.sexp(mission, "( is-destroyed-delay 0 !Alpha 1! ")
    assert r["value"] is False and "did not parse" in r["note"]


def test_bridge_reports_the_installed_version(mission, engine_env):
    # Contract C5 / QA finding TA-17: bridge_info names the copy the engine loaded. Needs the
    # version file bridge_install writes (group B's half of C5).
    fs2, _ = engine_env
    installed = fs2 / paths.BRIDGE_MOD / "data" / "text" / "fsoharness-version.txt"
    assert launcher.bridge_info(mission)["version"] == installed.read_text().strip()
