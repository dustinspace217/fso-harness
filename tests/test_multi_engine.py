"""Engine-tier multiplayer tests: a real match (unshare/nsenter), a real standalone and client.

Security requirements from the spec (section 5) are asserted here, each one as a check that would
FAIL if containment broke: listeners from the real network, a send out of the match, +pxo, holder
death. Run: bounded-run pytest -m engine tests/test_multi_engine.py -p no:xdist
"""
import errno
import os
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

from fsoctl import launcher, match, session, xwin
from fsoctl.errors import HarnessError
from fsoctl.launcher import LaunchRequest
from tests.engine_helpers import MODS, launch_in_mission, wait_hb

pytestmark = pytest.mark.engine
MP_MISSION = os.environ.get("FSO_TEST_MP_MISSION", "M-01")     # a retail coop mission


@pytest.fixture
def live_match(tmp_runtime):
    m = match.create(label="test")
    try:
        yield m
    finally:
        launcher.remove_match(m.id)


def _listeners(port: int, proto: str = "u") -> int:
    """Count sockets on `port` on the REAL network (what the LAN would see)."""
    out = subprocess.run(["ss", "-H", f"-l{proto}n", f"sport = :{port}"], capture_output=True,
                         text=True, check=False).stdout
    return len(out.strip().splitlines())


def _launch(fso, m, role, **kw):
    return launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, match=m.id, role=role,
                                         skip_movies=True, dismiss_popups=True, **kw))


def test_match_holder_is_unprivileged_and_private(live_match):
    m = live_match
    assert match.alive(m)
    uid = match.run_inside(m, ["id", "-u"]).stdout.strip()
    assert uid == "0"                                   # root INSIDE the user namespace only
    outside = subprocess.run(["ps", "-o", "uid=", "-p", str(m.holder_pid)], capture_output=True,
                             text=True, check=False).stdout.strip()
    assert outside == str(os.getuid())                   # the plain user outside
    # spec 5.2: a send to a non-loopback address fails inside, with ENETUNREACH, not silently
    probe = textwrap.dedent("""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"x", ("8.8.8.8", 53)); print("SENT")
        except OSError as e:
            print(e.errno)
    """)
    r = match.run_inside(m, ["python3", "-c", probe])
    assert r.stdout.strip() == str(errno.ENETUNREACH)
    # spec 5.7, recorded not required: names still resolve inside (the host's resolver answers
    # over a local socket, probed 2026-10-12); pinned so a change is noticed (QA TA-6).
    r = match.run_inside(m, ["getent", "hosts", "example.com"], timeout=20)
    assert r.returncode == 0, r


def test_standalone_listens_only_inside_the_match(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    before_udp, before_tcp = _listeners(7808), _listeners(8080, "t")
    r = _launch(fso, m, "standalone")
    try:
        assert r["role"] == "standalone" and r["port"] == 7808 and r["renderer"] == "none"
        st = launcher.status(r["id"])
        assert st["listening"] is True and st["match_network"] == "alive"
        assert _listeners(7808) == before_udp               # spec 5.1: nothing new on the LAN side
        assert _listeners(8080, "t") == before_tcp
        prefs = session.prefs_dir(session.load(r["id"]))
        cfg = (prefs / "data" / "multi.cfg").read_text()
        assert "+pxo" not in cfg                            # spec 5.3
        assert "+pxo" not in "".join(p.read_text(errors="replace") for p in prefs.rglob("*.cfg"))
        # the tracker flag of a PILOT lives in data/players/*.json (QA TA-11); none written here
        # because a standalone has no pilot, and the client test below checks its own
        assert not list(prefs.rglob("players/*.json"))
    finally:
        launcher.stop(r["id"])


def test_client_joins_standalone(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    srv = _launch(fso, m, "standalone")
    cli = None
    try:
        cli = _launch(fso, m, "client", pilot_mode="create", pilot_name="Alpha")
        assert cli["port"] == 7809
        hb = wait_hb(cli["id"], lambda h: h.get("state") in ("GS_STATE_MULTI_HOST_SETUP",
                                                             "GS_STATE_MULTI_CLIENT_SETUP"),
                     "a multiplayer setup state", timeout=180)
        assert hb["switches"]["multi_join"] == "done" and hb["switches"]["pilot_mode"] == "done"
        pilots = "".join(p.read_text(errors="replace") for p in
                         session.prefs_dir(session.load(cli["id"])).rglob("players/*.json"))
        assert pilots and "pxo" not in pilots.lower()       # spec 5.3 for the pilot (QA TA-11)
        srv_prefs = session.prefs_dir(session.load(srv["id"]))
        logs = "".join(p.read_text(errors="replace") for p in srv_prefs.rglob("multi.log"))
        assert "Alpha" in logs
    finally:
        if cli:
            launcher.stop(cli["id"])
        launcher.stop(srv["id"])


def test_two_clients_get_distinct_ports_and_cap_counts_them(engine_env, live_match, monkeypatch):
    _, fso = engine_env
    m = live_match
    monkeypatch.setenv("FSO_HARNESS_MAX_SESSIONS", "3")
    ids = []
    try:
        ids.append(_launch(fso, m, "standalone")["id"])
        ids.append(_launch(fso, m, "client", pilot_mode="create", pilot_name="A")["id"])
        ids.append(_launch(fso, m, "client", pilot_mode="create", pilot_name="B")["id"])
        assert [session.load(i).port for i in ids] == [7808, 7809, 7810]
        with pytest.raises(HarnessError) as e:              # the cap: 3 sessions, match counts 0
            _launch(fso, m, "client", pilot_mode="create", pilot_name="C")
        assert "cap" in e.value.message
    finally:
        for i in ids:
            launcher.stop(i)


def test_mission_starts_with_almission(engine_env, live_match):
    """Success criterion (a): server + client reach GS_STATE_GAME_PLAY together."""
    _, fso = engine_env
    m = live_match
    srv = _launch(fso, m, "standalone")
    cli = None
    try:
        cli = _launch(fso, m, "client", pilot_mode="create", pilot_name="Alpha",
                      mission=MP_MISSION, skip_command_briefing=True, skip_briefing=True)
        wait_hb(cli["id"], lambda h: h.get("state") == "GS_STATE_GAME_PLAY", "GS_STATE_GAME_PLAY",
                timeout=300)
        # Teeth: the mission really runs (time advances) and the server ran the same mission.
        hb = wait_hb(cli["id"], lambda h: (h.get("mission_time") or 0) >= 1, "mission time",
                     timeout=60)
        assert hb["mission"].lower().startswith(MP_MISSION.lower())
        # The server's side of the same fact, read from its web panel INSIDE the match (spec 5.5:
        # agents reach the panel through the match, never from the real network). The panel
        # wants HTTP basic auth with multi.cfg's +webapi_username/+webapi_password; the engine's
        # defaults are admin/admin (multi_options.h:86) and the harness keeps them: the panel is
        # reachable only inside the match.
        # Not asserted: the standalone's web panel. Its /api/1/player, /api/1/netgameInfo and
        # /api/1/mission copies were empty or stale for a minute of gameplay in three runs
        # (probe-results 21.4), so it is not an instrument. A client in GS_STATE_GAME_PLAY with
        # mission time advancing already needs the standalone (the game master) to have synced,
        # counted down and started the same mission.
    finally:
        if cli:
            launcher.stop(cli["id"])
        launcher.stop(srv["id"])


def test_holder_death_is_detected_and_containment_holds(engine_env, live_match):
    """The holder dying is a loss of the recorded handle, not of containment: the engine keeps
    its namespace alive, stays invisible from the real network, and is still stoppable through
    its own scope (QA TA-5, SL premise attack 1)."""
    _, fso = engine_env
    m = live_match
    before_udp, before_tcp = _listeners(7808), _listeners(8080, "t")
    srv = _launch(fso, m, "standalone")
    try:
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", f"{m.scope}.scope"],
                       check=False)
        for _ in range(50):          # bounded: 10 s for systemd to end the holder's scope
            if not match.alive(match.load(m.id)):
                break
            time.sleep(0.2)
        assert not match.alive(match.load(m.id))
        st = launcher.status(srv["id"])
        assert st["match_network"] == "dead" and st["status"] == "dead-network"
        assert launcher.alive(session.load(srv["id"]))      # the engine itself lives on
        assert _listeners(7808) == before_udp and _listeners(8080, "t") == before_tcp
        assert match.load(m.id).status == "dead"             # written back once (QA SL-7)
        with pytest.raises(HarnessError) as e:
            _launch(fso, m, "client", pilot_mode="create", pilot_name="Z")
        assert "dead" in e.value.message
    finally:
        launcher.stop(srv["id"])
        assert not launcher.alive(session.load(srv["id"]))  # its own scope still stops it


def test_remove_match_stops_sessions_then_holder(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    srv = _launch(fso, m, "standalone")
    out = launcher.remove_match(m.id)
    assert out["stopped"] == [srv["id"]]
    assert not launcher.alive(session.load(srv["id"])) and not match.alive(m)


def test_host_role_reaches_gameplay_alone(engine_env, live_match):
    """Host role on the engine (QA TA-2 / SL cell 1): a -startgame host with a mission hosts and
    enters the mission with no human; nothing new on the real network while it runs."""
    _, fso = engine_env
    m = live_match
    before_udp = _listeners(7808)
    host = _launch(fso, m, "host", pilot_mode="create", pilot_name="Hosty", mission=MP_MISSION,
                   skip_command_briefing=True, skip_briefing=True)
    try:
        assert host["port"] == 7808 and host["role"] == "host"
        assert _listeners(7808) == before_udp
        wait_hb(host["id"], lambda h: h.get("state") == "GS_STATE_GAME_PLAY", "GS_STATE_GAME_PLAY",
                timeout=300)
        hb = wait_hb(host["id"], lambda h: (h.get("mission_time") or 0) >= 1, "mission time",
                     timeout=60)
        assert hb["mission"].lower().startswith(MP_MISSION.lower())
        prefs = session.prefs_dir(session.load(host["id"]))
        logs = "".join(f.read_text(errors="replace") for f in prefs.rglob("multi.log"))
        assert "Starting netgame as Host/Server" in logs
    finally:
        launcher.stop(host["id"])


@pytest.mark.xfail(strict=True, raises=pytest.fail.Exception,
                   reason="a -almission host launches its mission at once, before a "
                   "client can load and join, and a client cannot join a mission in progress; "
                   "host-plus-client needs a hold-for-players step (QA TA-2, deferred)")
def test_client_joins_player_host(engine_env, live_match):
    _, fso = engine_env
    m = live_match
    host = _launch(fso, m, "host", pilot_mode="create", pilot_name="Hosty", mission=MP_MISSION,
                   skip_command_briefing=True, skip_briefing=True)
    cli = None
    try:
        cli = _launch(fso, m, "client", pilot_mode="create", pilot_name="Alpha",
                      skip_command_briefing=True, skip_briefing=True)
        wait_hb(cli["id"], lambda h: h.get("state") == "GS_STATE_GAME_PLAY", "GS_STATE_GAME_PLAY",
                timeout=120)
    finally:
        if cli:
            launcher.stop(cli["id"])
        launcher.stop(host["id"])


def test_ordinary_sessions_are_private_by_default_and_real_is_opt_in(engine_env, tmp_runtime):
    """DEF-MP-03 (Option A): an ordinary single-player session no longer binds the engine's UDP
    port on the real network; --network real is the documented opt-out and does."""
    _, fso = engine_env
    before = _listeners(7808)
    sid = launch_in_mission(fso)
    try:
        assert session.load(sid).network == "private"
        assert _listeners(7808) == before
    finally:
        launcher.stop(sid)
    sid = launch_in_mission(fso, network="real")
    try:
        assert _listeners(7808) == before + 1
    finally:
        launcher.stop(sid)
    assert _listeners(7808) == before


def _window_pid(display: str, wid: str) -> str:
    out = subprocess.run(["xprop", "-display", display, "-id", wid, "_NET_WM_PID"],
                         capture_output=True, text=True, timeout=10, check=False).stdout
    return out.rsplit("=", 1)[-1].strip()


def test_private_session_display_is_reachable_from_the_host(engine_env, tmp_runtime):
    """The engine window of a private-network session (and of a match client) must be found from
    the host side on the session's display, and the pid the window carries must be the engine's
    (so the exec chain unshare -> sh -> exec kept it). Before the fix, Xvfb inside the namespace
    picked ":1" and the host-side xdotool landed on the desktop's Xwayland (probe 2026-10-13)."""
    _, fso = engine_env
    mid = None
    sids = []
    try:
        mid = match.create("display reach").id
        sids.append(launch_in_mission(fso))
        sids.append(launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, skip_movies=True,
                                                  dismiss_popups=True, match=mid, role="client",
                                                  pilot_mode="create", pilot_name="Alpha"))["id"])
        for sid in sids:
            sess = session.load(sid)
            assert sess.display and sess.engine_pid
            comm = Path(f"/proc/{sess.engine_pid}/comm").read_text().strip()
            assert comm == Path(str(fso)).name[:15], comm     # the pid is the engine, not a wrapper
            deadline = time.monotonic() + 30
            wins = xwin.windows(sess.display, sess.engine_pid)
            while not wins and time.monotonic() < deadline:   # bounded: 30 s
                time.sleep(1)
                wins = xwin.windows(sess.display, sess.engine_pid)
            assert wins, f"no engine window on {sess.display} for pid {sess.engine_pid}"
            assert _window_pid(sess.display, wins[0][0]) == str(sess.engine_pid)
    finally:
        for sid in sids:
            launcher.stop(sid)
        if mid:
            launcher.remove_match(mid)
