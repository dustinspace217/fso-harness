"""Ports held outside the harness (QA CR-1/TA-3/SL-1, CR-2/SA-2/SL-2, CR-6/SL-3).

The harness's records know only its own sessions under one runtime root. A stock game, another
runtime root or any program on a port leaves the engine without networking (UDP) or without a
web panel (TCP), because the engine carries on after a failed bind. So a port is handed out only
if a test bind succeeds (port_free), readiness counts only the engine's own socket
(engine_holds), and the panel's login is reported only while the engine holds the panel.
"""
import socket
import subprocess

import pytest

from fsoctl import launcher, match, platform, session
from fsoctl.errors import EXIT_LAUNCH, EXIT_USAGE, HarnessError


def _sess(**kw):
    """A game session record with overrides."""
    base = dict(kind="game", binary="/x/fs2_open", fs2path="/g", mods=[], mission=None,
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[])
    base.update(kw)
    return session.new_session(**base)


def _alive(monkeypatch, *live):
    names = {s.scope for s in live}
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: unit not in names)


@pytest.mark.linux
@pytest.mark.host_ports
def test_port_free_sees_a_udp_socket_the_way_the_engine_binds():
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)      # an IPv4-only program
    held.bind(("0.0.0.0", 0))
    port = held.getsockname()[1]
    try:
        assert launcher.port_free("udp", port) is False         # the dual-stack bind collides
    finally:
        held.close()
    assert launcher.port_free("udp", port) is True


@pytest.mark.linux
@pytest.mark.host_ports
def test_port_free_sees_a_tcp_listener():
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("0.0.0.0", 0))
    held.listen(1)
    port = held.getsockname()[1]
    try:
        assert launcher.port_free("tcp", port) is False
    finally:
        held.close()
    assert launcher.port_free("tcp", port) is True


def test_host_port_skips_a_port_the_host_refuses(tmp_runtime, monkeypatch):
    taken = {7808, 7809}
    monkeypatch.setattr(launcher, "port_free", lambda proto, port: port not in taken)
    _alive(monkeypatch)
    assert launcher.host_port(None) == 7810
    with pytest.raises(HarnessError) as e:
        launcher.host_port(7808)
    assert e.value.code == EXIT_USAGE and "another program" in e.value.message
    monkeypatch.setattr(launcher, "port_free", lambda proto, port: False)
    with pytest.raises(HarnessError) as e:
        launcher.host_port(None)
    assert e.value.code == EXIT_LAUNCH                    # bounded: no endless search


def test_web_port_skips_a_port_the_host_refuses(tmp_runtime, monkeypatch):
    seen = []
    monkeypatch.setattr(launcher, "port_free",
                        lambda proto, port: seen.append(proto) or port != 8080)
    _alive(monkeypatch)
    assert launcher.web_port() == 8081 and set(seen) == {"tcp"}
    monkeypatch.setattr(launcher, "port_free", lambda proto, port: False)
    with pytest.raises(HarnessError) as e:
        launcher.web_port()
    assert e.value.code == EXIT_LAUNCH


def test_old_real_records_without_a_port_hold_the_default(tmp_runtime, monkeypatch):
    # Before Task 1 a real-network session had no port and bound 7808; so did a record from
    # before the network field existed (network None, not in a match).
    old_real = _sess(network="real")
    _alive(monkeypatch, old_real)
    assert launcher.host_port(None) == 7809
    older = _sess()
    _alive(monkeypatch, older)
    assert launcher.host_port(None) == 7809
    for quiet in (_sess(network="private"), _sess(kind="qtfred", network="real"),
                  _sess(match="m0a1b2c3d", role="client")):
        _alive(monkeypatch, quiet)
        assert launcher.host_port(None) == 7808, quiet


@pytest.mark.linux
def test_panel_not_reported_unless_this_engine_holds_it(tmp_runtime, monkeypatch):
    m = match.create()
    srv = _sess(match=m.id, role="standalone", port=7808, network="real", web_port=8080,
                web_user="fsoh-a1b2c3", web_password="pw")
    srv.engine_pid = 4242
    session.save(srv)
    _alive(monkeypatch, srv)
    lines = {"udp": "pid=4242,", "tcp": "pid=999,"}             # someone else has 8080

    def fake_run(mm, argv, timeout=10):
        out = lines["udp" if "-lunp" in argv else "tcp"]
        line = f'x users:(("p",{out}fd=3))\n'
        return launcher.subprocess.CompletedProcess(argv, 0, stdout=line, stderr="")
    monkeypatch.setattr(match, "run_inside", fake_run)
    st = launcher.status(srv.id)
    assert st["listening"] is True and st["web_panel"] is None
    assert [k for k in st if k.startswith("web_")] == ["web_panel"]   # the login appears once
    monkeypatch.setattr(launcher, "_start", lambda req: (srv, False))
    monkeypatch.setattr(launcher, "_wait_ready", lambda req, sid: {})
    monkeypatch.setattr(launcher, "_WEB_PANEL_WAIT", 0.0)
    out = launcher.launch(launcher.LaunchRequest(binary="/bin/true", match=m.id, role="standalone"))
    assert out["web_panel"] is None and any("8080" in w for w in out["warnings"])
    lines["tcp"] = "pid=4242,"
    assert launcher.status(srv.id)["web_panel"]["password"] == "pw"


def test_web_root_must_survive_the_engines_reader_and_cgi_lookup():
    # QA XFA-1/XFA-2: a cut line or a .php folder above the root reopens SA-1.
    for bad in ("/run/u/fso.php/s1/webui", "/x/Cache.CGI/webui", "/a\r", "/" + "a" * 520):
        with pytest.raises(HarnessError):
            launcher.multi_cfg_text("g", 8080, "u", "p", bad)
    assert "+webui_root /run/user/1000/fso-harness/s1/webui" in \
        launcher.multi_cfg_text("g", 8080, "u", "p", "/run/user/1000/fso-harness/s1/webui")


@pytest.mark.linux
@pytest.mark.host_ports
def test_port_free_without_ipv6_does_not_pretend(monkeypatch):
    # QA XFA-5: the engine opens only an IPv6 socket; with none, no port choice matters.
    real = socket.socket

    def no_v6(family=socket.AF_INET, *a, **k):
        if family == socket.AF_INET6:
            raise OSError(97, "Address family not supported by protocol")
        return real(family, *a, **k)
    held = real(socket.AF_INET, socket.SOCK_DGRAM)      # an IPv4 program holds the port
    held.bind(("0.0.0.0", 0))
    port = held.getsockname()[1]
    monkeypatch.setattr(launcher.socket, "socket", no_v6)
    try:
        # The old IPv4 fallback said "taken"; the engine could not use any port anyway.
        assert launcher.port_free("udp", port) is True
    finally:
        held.close()


def test_a_host_that_lost_its_port_fails_and_others_warn(tmp_runtime, monkeypatch):
    # QA XFA-3: after ready, a real-network engine must hold its game port.
    monkeypatch.setattr(launcher, "_GAME_PORT_WAIT", 0.0)
    monkeypatch.setattr(launcher, "engine_holds", lambda sess, proto, port: False)
    host = _sess(match="m0a1b2c3d", role="host", port=7808, network="real")
    with pytest.raises(HarnessError) as e:
        launcher._check_game_port(host, [])
    assert e.value.code == EXIT_LAUNCH and "7808" in e.value.message
    warnings = []
    launcher._check_game_port(_sess(network="real", port=7809), warnings)
    assert warnings and "7809" in warnings[0]
    quiet = []
    launcher._check_game_port(_sess(network="private"), quiet)
    launcher._check_game_port(_sess(match="m0a1b2c3d", role="standalone", port=7810,
                                    network="real"), quiet)
    assert quiet == []
    monkeypatch.setattr(launcher, "engine_holds", lambda sess, proto, port: True)
    launcher._check_game_port(host, quiet)
    assert quiet == []


@pytest.mark.linux
def test_engine_holds_asks_the_host_for_an_ordinary_session(tmp_runtime, monkeypatch):
    seen = []

    def fake_run(argv, **kw):
        seen.append(argv)
        return launcher.subprocess.CompletedProcess(argv, 0, 'x users:(("p",pid=77,fd=3))\n', "")
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    s = _sess(network="real", port=7808)
    s.engine_pid = 77
    assert launcher.engine_holds(s, "udp", 7808) is True
    assert seen == [["ss", "-H", "-lunp", "sport = :7808"]]


# ---- pinned by the second mutation pass (plan A QA, fix-wave lines) -------------------------

def test_host_port_tests_udp(tmp_runtime, monkeypatch):
    protos = []
    monkeypatch.setattr(launcher, "port_free", lambda proto, port: protos.append(proto) or True)
    _alive(monkeypatch)
    launcher.host_port(None)
    launcher.host_port(7900)
    assert protos == ["udp", "udp"]


@pytest.mark.linux
def test_engine_holds_edges(tmp_runtime, monkeypatch):
    calls = []

    def fake_inside(mm, argv, timeout=10):
        calls.append(argv)
        return launcher.subprocess.CompletedProcess(argv, 0, 'x users:(("p",pid=5,fd=3))\n', "")
    monkeypatch.setattr(match, "run_inside", fake_inside)
    monkeypatch.setattr(match, "load", lambda mid: object())
    s = _sess(match="m0a1b2c3d", role="standalone", port=7808)
    s.engine_pid = 5
    assert launcher.engine_holds(s, "tcp", 8080) is True
    assert calls[-1] == ["ss", "-H", "-ltnp", "sport = :8080"]
    assert launcher.engine_holds(s, "udp", None) is False          # no port: nothing to own
    s.engine_pid = None
    assert launcher.engine_holds(s, "udp", 7808) is False          # not started yet
    assert len(calls) == 1                                          # neither asked ss

    def broken(mm, argv, timeout=10):
        raise HarnessError(launcher.EXIT_REFUSED, "match is dead", "")
    s.engine_pid = 5
    monkeypatch.setattr(match, "run_inside", broken)
    assert launcher.engine_holds(s, "udp", 7808) is False


@pytest.mark.linux
def test_engine_holds_host_query_is_bounded_text(tmp_runtime, monkeypatch):
    def fake_run(argv, **kw):
        assert kw == {"capture_output": True, "text": True, "timeout": 10, "check": False}, kw
        return launcher.subprocess.CompletedProcess(argv, 0, 'x users:(("p",pid=9,fd=3))\n', "")
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    s = _sess(network="real", port=7808)
    s.engine_pid = 9
    assert launcher.engine_holds(s, "udp", 7808) is True


def test_web_panel_up_polls_the_tcp_port_until_the_deadline(tmp_runtime, monkeypatch):
    answers = [False, False, True]
    asked = []
    monkeypatch.setattr(launcher, "engine_holds",
                        lambda sess, proto, port: asked.append(proto) or answers.pop(0))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    s = _sess(match="m0a1b2c3d", role="standalone", port=7808, web_port=8080)
    assert launcher.web_panel_up(s, wait=5.0) is True and set(asked) == {"tcp"}
    answers[:] = [False]
    assert launcher.web_panel_up(s) is False                      # no wait: one look only


@pytest.mark.linux
@pytest.mark.host_ports
def test_port_free_reports_other_bind_errors(monkeypatch):
    class Refusing:
        def __init__(self, *a, **k):
            pass

        def setsockopt(self, *a):
            pass

        def bind(self, addr):
            raise OSError(13, "Permission denied")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(launcher.socket, "socket", Refusing)
    for proto in ("udp", "tcp"):
        with pytest.raises(HarnessError) as e:
            launcher.port_free(proto, 7808)
        assert e.value.code == EXIT_LAUNCH and proto in e.value.message


@pytest.mark.linux
def test_status_checks_sockets_only_for_a_live_standalone(tmp_runtime, monkeypatch):
    m = match.create()
    cli = _sess(match=m.id, role="client", port=7809, network="real")
    _alive(monkeypatch, cli)
    monkeypatch.setattr(launcher, "engine_holds",
                        lambda sess, proto, port: pytest.fail("a client's sockets are not read"))
    st = launcher.status(cli.id)
    assert st["listening"] is None and st["web_panel"] is None
    assert "alive" in st and "heartbeat" in st and st["match_network"] == "alive"


@pytest.mark.host_ports        # the real port_free, not the unit tier's stub (conftest)
def test_on_windows_a_port_is_tested_in_the_socket_table_and_nothing_is_bound(monkeypatch):
    # A test bind there makes Windows Firewall add a rule for Python and ask the user about
    # it (measured in the VM, 2026-11-16: probe-results section 23); a session must never
    # put a window in front of the user.
    monkeypatch.setattr(platform, "NAME", "windows")

    def no_sockets(*a, **kw):
        raise AssertionError("opened a socket to test a port on Windows")
    monkeypatch.setattr(launcher.socket, "socket", no_sockets)
    asked = []

    def owners(proto, port, run=None):
        asked.append((proto, port))
        return {4242} if port == 7808 else set()
    monkeypatch.setattr(launcher.procs, "socket_owner_pids", owners)
    assert launcher.port_free("udp", 7808) is False and launcher.port_free("udp", 7809) is True
    assert launcher.port_free("tcp", 8080) is True
    assert asked == [("udp", 7808), ("udp", 7809), ("tcp", 8080)]


NETSTAT_ONE_HOLDER = "  UDP    0.0.0.0:7808           *:*                                    4242\n"


def _windows_listing(monkeypatch, answer):
    """port_free as Windows runs it, with the real netstat lookup and `answer` in place of
    running netstat (a CompletedProcess to return, or an exception to raise)."""
    from fsoctl.platform import win_procs  # noqa: PLC0415
    monkeypatch.setattr(platform, "NAME", "windows")
    monkeypatch.setattr(launcher, "procs", win_procs)
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(launcher.subprocess, "run", run)
    return seen


@pytest.mark.host_ports
def test_on_windows_the_listing_decides_and_is_read_whatever_its_bytes(monkeypatch):
    seen = _windows_listing(monkeypatch, subprocess.CompletedProcess([], 0, NETSTAT_ONE_HOLDER, ""))
    assert launcher.port_free("udp", 7808) is False and launcher.port_free("udp", 7809) is True
    argv, kw = seen[0]
    assert argv[0].endswith(r"\System32\netstat.exe") and argv[1:] == ["-ano"]
    # errors="replace": the listing's headers are in the system's language and code page;
    # only its digits and the protocol column are read (QA Plan B, CR-5)
    assert kw == {"capture_output": True, "text": True, "errors": "replace", "timeout": 10,
                  "check": False}


@pytest.mark.host_ports
@pytest.mark.parametrize("answer", [
    subprocess.CompletedProcess([], 1, "", "access denied"),
    FileNotFoundError(2, "The system cannot find the file specified"),
    subprocess.TimeoutExpired("netstat", 10),
])
def test_on_windows_a_listing_that_cannot_be_had_is_an_error_not_a_free_port(monkeypatch,
                                                                            answer):
    # QA Plan B, CR-4 / ST-9: the lookup's own answer to a failed listing is "no owners",
    # right for "does my engine hold this port" and wrong here: every port read as free and
    # every session was handed the same one.
    _windows_listing(monkeypatch, answer)
    with pytest.raises(HarnessError) as e:
        launcher.port_free("udp", 7808)
    assert e.value.code == EXIT_LAUNCH and "cannot list the machine's sockets" in e.value.message
    assert "netstat" in e.value.message and "-ano" not in e.value.message   # names the program


def test_a_port_is_the_sessions_only_when_its_own_engine_holds_it(tmp_runtime, monkeypatch):
    # QA Plan B, TA-13: the three tests of this check above run the Linux listing tool and
    # carry a Linux mark; the decision itself is the same on every system, and Windows uses
    # it for every session's game port.
    asked = []

    def owners(proto, port, run=None):
        asked.append((proto, port, run))
        return {77, 90}
    monkeypatch.setattr(launcher.procs, "socket_owner_pids", owners)
    s = _sess(network="real", port=7808)
    s.engine_pid = 77
    assert launcher.engine_holds(s, "udp", 7808) is True
    s.engine_pid = 78                               # another program holds the port
    assert launcher.engine_holds(s, "udp", 7808) is False
    assert asked == [("udp", 7808, None)] * 2       # asked on the host, not inside a match
    s.engine_pid = None
    assert launcher.engine_holds(s, "udp", 7808) is False
    s.engine_pid = 77
    assert launcher.engine_holds(s, "udp", None) is False

    def broken(proto, port, run=None):
        raise OSError("the listing tool is gone")
    monkeypatch.setattr(launcher.procs, "socket_owner_pids", broken)
    assert launcher.engine_holds(s, "udp", 7808) is False       # not a failed status read
