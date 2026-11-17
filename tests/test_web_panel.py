"""The standalone's web panel: its own port, never the default login (cross-platform Plan A,
Task 3).

The standalone server opens a web panel (basic auth) on TCP, port 8080 and login admin/admin
unless multi.cfg says otherwise (multi_options.h defaults; multi_options.cpp:294-318 reads
+webapi_username, +webapi_password, +webapi_server_port). On the real network that panel is on
the LAN, so every standalone gets a random login and a port no other live real-network
standalone holds. The agent reads the login from the launch result and `status`; it must never
reach the audit log or a journal.
"""
import json

import pytest

from fsoctl import launcher, match, paths, record, session
from fsoctl.launcher import LaunchRequest


def _fs2(tmp_runtime, monkeypatch):
    """Point FS2PATH at an empty game folder and make the bridge install a no-op."""
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    game = tmp_runtime.parent / "FS2"
    game.mkdir()
    monkeypatch.setenv("FS2PATH", str(game))


def _alive(monkeypatch, *live):
    """Make exactly the given sessions' scopes count as live."""
    names = {s.scope for s in live}
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: unit not in names)


def _standalone(network="real"):
    """Prepare a standalone in a new match of the given network (holder stubbed for private)."""
    m = match.create() if network == "real" else None
    if m is None:
        m = match.Match(id="m0a1b2c3d", scope=match.scope_name("m0a1b2c3d"), network="private",
                        holder_pid=1, holder_start=1, status="alive")
        match.save(m)
    sess, _ = launcher._prepare(LaunchRequest(binary="/bin/true", match=m.id, role="standalone"))
    return sess


def test_multi_cfg_never_default_login():
    text = launcher.multi_cfg_text("g", 8081, "fsoh-a1b2c3", "pw", "/r/webui")
    lines = text.splitlines()
    assert "+webapi_server_port 8081" in lines
    assert "+webapi_username fsoh-a1b2c3" in lines and "+webapi_password pw" in lines
    # a role with no web panel gets no web lines at all
    assert "webapi" not in launcher.multi_cfg_text("g", None, None, None)


def test_standalone_gets_a_random_login(tmp_runtime, monkeypatch):
    _fs2(tmp_runtime, monkeypatch)
    _alive(monkeypatch)
    a = _standalone()
    assert a.web_user.startswith("fsoh-") and len(a.web_user) == 11
    assert len(a.web_password) == 24 and "admin" not in (a.web_user, a.web_password)
    cfg = (session.prefs_dir(a) / "data" / "multi.cfg").read_text().splitlines()
    assert f"+webapi_username {a.web_user}" in cfg and f"+webapi_password {a.web_password}" in cfg
    assert f"+webapi_server_port {a.web_port}" in cfg
    _alive(monkeypatch, a)
    b = _standalone()
    assert b.web_password != a.web_password


def test_two_standalones_get_distinct_web_ports(tmp_runtime, monkeypatch):
    _fs2(tmp_runtime, monkeypatch)
    _alive(monkeypatch)
    a = _standalone()
    _alive(monkeypatch, a)
    b = _standalone()
    assert (a.web_port, b.web_port) == (8080, 8081)
    _alive(monkeypatch, b)                       # the first one stopped: 8080 is free again
    assert launcher.web_port() == 8080


def test_private_standalone_port_stays_inside_its_namespace(tmp_runtime, monkeypatch):
    _fs2(tmp_runtime, monkeypatch)
    monkeypatch.setattr(match, "alive", lambda m: True)
    _alive(monkeypatch)
    real = _standalone()
    _alive(monkeypatch, real)
    private = _standalone("private")
    assert real.web_port == 8080 and private.web_port == 8080
    assert private.web_user.startswith("fsoh-")    # never the default login, private or not


def test_clients_and_hosts_get_no_web_panel(tmp_runtime, monkeypatch):
    _fs2(tmp_runtime, monkeypatch)
    _alive(monkeypatch)
    srv = _standalone()
    _alive(monkeypatch, srv)
    cli, _ = launcher._prepare(LaunchRequest(binary="/bin/true", match=srv.match, role="client",
                                             pilot_mode="create"))
    assert cli.web_port is None and cli.web_password is None
    assert "webapi" not in (session.prefs_dir(cli) / "data" / "multi.cfg").read_text()
    assert launcher.web_panel(cli) is None


def test_launch_result_and_status_report_web_panel(tmp_runtime, monkeypatch):
    _fs2(tmp_runtime, monkeypatch)
    _alive(monkeypatch)
    srv = _standalone()
    _alive(monkeypatch, srv)
    want = {"port": srv.web_port, "user": srv.web_user, "password": srv.web_password}
    monkeypatch.setattr(launcher, "_start", lambda req: (srv, False))
    monkeypatch.setattr(launcher, "_wait_ready", lambda req, sid: {})
    monkeypatch.setattr(launcher, "web_panel_up", lambda sess, wait=0.0: True)
    out = launcher.launch(LaunchRequest(binary="/bin/true", match=srv.match, role="standalone"))
    assert out["web_panel"] == want
    monkeypatch.setattr(launcher, "listening", lambda s: True)
    assert launcher.status(srv.id)["web_panel"] == want


def test_web_password_not_in_audit_or_journal(tmp_runtime, monkeypatch):
    # QA TA-4: capture every write to the audit logs and the journal through the whole life of a
    # standalone (prepare, launch, status, list, stop, remove), instead of reading files that may
    # never be created.
    written = []
    for mod, name in ((session, "audit"), (match, "audit"), (record, "write")):
        real = getattr(mod, name)

        def spy(*args, _real=real, **kw):
            written.append(repr(args) + repr(kw))
            return _real(*args, **kw)
        monkeypatch.setattr(mod, name, spy)
    _fs2(tmp_runtime, monkeypatch)
    _alive(monkeypatch)
    srv = _standalone()
    _alive(monkeypatch, srv)
    monkeypatch.setattr(launcher, "_start", lambda req: (srv, False))
    monkeypatch.setattr(launcher, "_wait_ready", lambda req, sid: {})
    monkeypatch.setattr(launcher, "web_panel_up", lambda sess, wait=0.0: True)
    monkeypatch.setattr(launcher, "listening", lambda s: True)
    out = launcher.launch(LaunchRequest(binary="/bin/true", match=srv.match, role="standalone"))
    assert out["web_panel"]["password"] == srv.web_password      # the agent can read it...
    launcher.status(srv.id)
    assert srv.web_password not in json.dumps(launcher.list_all())
    monkeypatch.setattr(launcher.scope, "stop", lambda unit, timeout=20: None)
    monkeypatch.setattr(launcher.scope, "state", lambda unit: "inactive")
    launcher.remove_match(srv.match)
    assert written, "no audit line was written at all, so this test would prove nothing"
    assert not any(srv.web_password in w for w in written)      # ...and no log ever holds it
    for f in (paths.state_dir() / "audit.log", session.run_dir(srv) / "audit.log",
              session.run_dir(srv) / record.JOURNAL):
        assert not f.exists() or srv.web_password not in f.read_text(), f


def test_standalone_web_root_is_an_empty_folder_of_its_own(tmp_runtime, monkeypatch):
    # QA SA-1: every standalone gets an empty folder in its run folder as its web root.
    _fs2(tmp_runtime, monkeypatch)
    _alive(monkeypatch)
    srv = _standalone()
    cfg = (session.prefs_dir(srv) / "data" / "multi.cfg").read_text().splitlines()
    roots = [line.split(" ", 1)[1] for line in cfg if line.startswith("+webui_root ")]
    assert roots == [str(session.run_dir(srv) / "webui")]
    root = session.run_dir(srv) / "webui"
    assert root.is_dir() and list(root.iterdir()) == []
    assert root.stat().st_mode & 0o077 == 0
    _alive(monkeypatch, srv)
    monkeypatch.setattr(match, "alive", lambda m: True)      # the stub private match's holder
    private = _standalone("private")
    assert any(line.startswith("+webui_root ") for line in
               (session.prefs_dir(private) / "data" / "multi.cfg").read_text().splitlines())


def test_web_root_is_required_with_a_panel():
    for root in (None, "", "/a\n+pxo"):
        with pytest.raises(launcher.HarnessError):
            launcher.multi_cfg_text("g", 8080, "u", "p", root)
