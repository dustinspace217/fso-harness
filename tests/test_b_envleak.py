"""Engine tier (QA TA-18, fix-wave-2 R2): the REAL engine and qtFRED run with the private HOME
and runtime folder, and never join the user's session bus (QA SA-2, SA-3, CR-2, SA2-1).
child_env is unit-tested; these check what the processes actually received and did:
/proc/<pid>/environ, and `busctl --user list` (the bus's own list of connected clients, the
check from probes/fsoh-probe-dbus.py; an ss socket listing was blind to client sockets).

Named test_b_envleak.py (not *_engine.py) because group A owns that file pattern.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

from fsoctl import fred, launcher, session
from fsoctl.launcher import LaunchRequest

MODS = [os.environ.get("FSO_TEST_MODS", "MVPS-4.7.3")]


def _environ(pid: int) -> dict[str, str]:
    """Return a process's environment from /proc (NUL-separated KEY=VALUE entries)."""
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return dict(e.decode().split("=", 1) for e in raw.split(b"\0") if b"=" in e)


def _on_session_bus(pid: int) -> list[str]:
    """Return `busctl --user list` rows whose PID column is `pid` (clients of the user's bus)."""
    out = subprocess.run(["busctl", "--user", "list", "--no-pager", "--no-legend"],
                         capture_output=True, text=True, timeout=20, check=True).stdout
    return [ln for ln in out.splitlines() if len(ln.split()) > 1 and ln.split()[1] == str(pid)]


def _assert_isolated(sess: session.Session) -> None:
    """Assert the session's process has the private environment and is not on the session bus."""
    env = _environ(sess.engine_pid)
    run = session.run_dir(sess)
    assert Path(env["HOME"]) == run / "home"
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        assert Path(env[var]).is_relative_to(run), var
    assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={run / 'no-bus'}"
    assert not (run / "no-bus").exists()
    for var in ("SESSION_MANAGER", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP",
                "KDE_FULL_SESSION", "DESKTOP_SESSION", "XDG_SESSION_DESKTOP"):
        assert var not in env, var
    assert env["SDL_VIDEO_ALLOW_SCREENSAVER"] == "1"
    assert env["GDK_BACKEND"] == "x11" and env["QT_QPA_PLATFORM"] == "xcb"
    assert env["DISPLAY"] == sess.display
    # The probe saw the engine on the bus at once and still 3 s later; check twice the same way.
    assert _on_session_bus(sess.engine_pid) == []
    time.sleep(3)
    assert _on_session_bus(sess.engine_pid) == []


@pytest.mark.engine
def test_engine_is_isolated_from_the_desktop(engine_env, tmp_runtime):
    _, fso = engine_env
    r = launcher.launch(LaunchRequest(binary=str(fso), skip_movies=True, mods=MODS))
    try:
        _assert_isolated(session.load(r["id"]))
    finally:
        launcher.stop(r["id"])


@pytest.mark.qtfred
def test_qtfred_is_isolated_from_the_desktop(tmp_runtime):
    if not os.environ.get("QTFRED_BIN") or not os.environ.get("FS2PATH"):
        pytest.skip("needs QTFRED_BIN and FS2PATH")
    q = fred.open(os.environ.get("FSO_TEST_MISSION", "SM1-01.fs2"), mods=MODS)
    try:
        _assert_isolated(session.load(q["id"]))
    finally:
        launcher.stop(q["id"])
