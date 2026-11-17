"""Matches on the real network by default (cross-platform Plan A, Task 2; owner's ruling
2026-10-16: "Real network. I never meant to change this about how the engine works.").

A real match has no holder and no namespace: it is a record that groups sessions. Its sessions
bind their game ports on the host, so their ports follow host_port's rule (unique across every
live real-network session on the machine), not the per-match rule a private namespace allows.
A private match (`--network private`) works exactly as before.
"""
import json
import subprocess

import pytest

from fsoctl import cli, launcher, match, paths, runner, scope, session
from fsoctl.errors import EXIT_REFUSED, EXIT_USAGE, HarnessError
from fsoctl.launcher import LaunchRequest


def _member(mid, role, port, network="real"):
    """A session record in match `mid` with the given role and port."""
    return session.new_session(kind="game", binary="/x/fs2_open", fs2path="/g", mods=[],
                               mission=None, renderer="opengl", width=1280, height=720,
                               switches={}, allow_eval=False, extra_args=[], match=mid,
                               role=role, port=port, network=network)


def _alive(monkeypatch, *live):
    """Make exactly the given sessions' scopes count as live."""
    names = {s.scope for s in live}
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: unit not in names)


def test_real_match_has_no_holder(tmp_runtime, monkeypatch):
    started = []
    monkeypatch.setattr(scope, "start", lambda *a, **k: started.append(a))
    m = match.create(label="r")
    assert m.network == "real" and m.status == "alive"
    assert m.holder_pid is None and started == []
    assert match.alive(m)
    assert match.nsenter_prefix(m) == []
    stopped = []
    monkeypatch.setattr(scope, "stop", stopped.append)
    match.end_holder(m)
    assert stopped == [] and match.load(m.id).status == "removed"
    assert not match.alive(match.load(m.id))


def test_real_match_sessions_get_host_unique_ports(tmp_runtime, monkeypatch):
    a, b = match.create(), match.create()
    server_a = _member(a.id, "standalone", launcher.assign_port(a.id, None))
    _alive(monkeypatch, server_a)
    server_b = _member(b.id, "standalone", launcher.assign_port(b.id, None))
    assert (server_a.port, server_b.port) == (7808, 7809)
    # an ordinary real-network session counts too, and a taken wanted port is refused
    _alive(monkeypatch, server_a, server_b)
    assert launcher.host_port(None) == 7810
    with pytest.raises(HarnessError) as e:
        launcher.assign_port(b.id, 7808)
    assert e.value.code == EXIT_USAGE


def test_real_match_client_connects_to_its_server(tmp_runtime, monkeypatch):
    a, b = match.create(), match.create()
    server_a = _member(a.id, "standalone", 7808)
    server_b = _member(b.id, "host", 7809)
    _alive(monkeypatch, server_a, server_b)
    assert launcher.default_connect(b.id) == "127.0.0.1:7809"
    # no live server: refused, because 7808 on the host may be another match's server
    _alive(monkeypatch, server_b)
    with pytest.raises(HarnessError) as e:
        launcher.default_connect(a.id)
    assert e.value.code == EXIT_USAGE


def test_private_match_unchanged(tmp_runtime, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        monkeypatch.setattr(scope, "start",
                            lambda unit, argv, env, log, props: type("P", (), {"pid": sleeper.pid}))
        monkeypatch.setattr(scope, "is_active", lambda unit: True)
        up = subprocess.CompletedProcess([], 0, stdout="lo UNKNOWN <LOOPBACK,UP>\n", stderr="")
        monkeypatch.setattr(match, "run_inside", lambda m, argv, timeout=10: up)
        m = match.create(network="private")
        assert m.network == "private" and m.holder_pid == sleeper.pid
        assert match.nsenter_prefix(m)[:2] == ["nsenter", "--preserve-credentials"]
        # ports stay per match: a private server takes 7808 even while a real session holds it
        real = _member(None, None, 7808)
        _alive(monkeypatch, real)
        assert launcher.assign_port(m.id, None) == 7808          # QA TA-5: host ports ignored
        inside = _member(m.id, "standalone", 7808, network="private")
        _alive(monkeypatch, real, inside)
        assert launcher.assign_port(m.id, None) == 7809
        assert launcher.default_connect(m.id) == "127.0.0.1:7808"
    finally:
        sleeper.kill()
        sleeper.wait()


def test_old_match_record_loads_private(tmp_runtime):
    m = match.create()
    f = paths.match_dir(m.id) / "match.json"
    data = json.loads(f.read_text())
    data.pop("network")
    f.write_text(json.dumps(data))
    assert match.load(m.id).network == "private"


def test_runner_skips_nsenter_for_real_match(tmp_runtime, monkeypatch):
    m = match.create()
    s = _member(m.id, "client", 7810)
    monkeypatch.setattr(launcher, "build_argv", lambda sess: ["fs2"])
    assert runner.engine_argv(s) == ["fs2"]


def test_match_session_takes_its_match_network(tmp_runtime):
    real = match.create()
    req = LaunchRequest(binary="/bin/true", match=real.id, role="client")
    assert launcher.resolve_network(req) == "real"
    # an explicit --network on a match session must agree with the match
    bad = LaunchRequest(binary="/bin/true", match=real.id, role="client", network="private")
    with pytest.raises(HarnessError) as e:
        launcher._check_network(bad)
    assert e.value.code == EXIT_USAGE


def test_removing_real_match_refuses_new_sessions(tmp_runtime):
    m = match.create()
    m.status = "removing"
    match.save(m)
    assert not match.alive(m)
    with pytest.raises(HarnessError) as e:
        match.nsenter_prefix(m)
    assert e.value.code == EXIT_REFUSED


def test_cli_match_create_network(tmp_runtime, capsys):
    assert cli.main(["--json", "match", "create"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["network"] == "real" and out["holder_pid"] is None
    cli.main(["--json", "match", "list"])
    assert json.loads(capsys.readouterr().out)["matches"][0]["network"] == "real"
    assert cli.main(["--json", "match", "create", "--network", "nope"]) == EXIT_USAGE


def test_prepare_a_real_match_server_then_client(tmp_runtime, monkeypatch):
    # The server goes into an EMPTY real match, so nothing may ask it for a server to join
    # (only a client joins); then the client defaults to that server's host port.
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    game = tmp_runtime.parent / "FS2"       # fs2path() wants an existing folder
    game.mkdir()
    monkeypatch.setenv("FS2PATH", str(game))
    m = match.create()
    other = _member(None, None, 7808)       # an ordinary real session already holds 7808
    _alive(monkeypatch, other)
    srv, _ = launcher._prepare(LaunchRequest(binary="/bin/true", match=m.id, role="standalone"))
    assert srv.network == "real" and srv.port == 7809
    _alive(monkeypatch, other, srv)
    cli, _ = launcher._prepare(LaunchRequest(binary="/bin/true", match=m.id, role="client",
                                             pilot_mode="create"))
    assert cli.network == "real" and cli.port == 7810 and cli.connect == "127.0.0.1:7809"
    assert launcher.build_argv(cli)[-2:] == ["-connect", "127.0.0.1:7809"]


def test_an_empty_real_match_ends_a_day_after_creation(tmp_runtime, monkeypatch):
    # QA CR-4/TA-6/SL-4: a real match has no holder whose 24 h limit ends it, so observe does.
    old = match.create(label="old")
    old.created -= match.HOLDER_RUNTIME_SEC + 1
    match.save(old)
    busy = match.create(label="busy")
    busy.created -= match.HOLDER_RUNTIME_SEC + 1
    match.save(busy)
    member = _member(busy.id, "client", 7808)
    member.status = "running"     # live by its scope alone, not by the launch grace
    session.save(member)
    _alive(monkeypatch, member)
    match.create(label="fresh")              # create() observes the others for its cap count
    states = {m.label: match.observe(m).status for m in match.list_matches()}
    assert states == {"old": "dead", "busy": "alive", "fresh": "alive"}
    audit = (paths.state_dir() / "audit.log").read_text()
    assert audit.count('"match_expired"') == 1
    assert match.observe(match.load(old.id)).status == "dead"      # written back once
    assert (paths.state_dir() / "audit.log").read_text().count('"match_expired"') == 1
    # the cap counts only live matches, so expired ones free their slots
    monkeypatch.setenv("FSO_HARNESS_MAX_MATCHES", "3")
    match.create(label="third")
    with pytest.raises(HarnessError):
        match.create(label="fourth")


def test_remove_match_on_a_real_match(tmp_runtime, monkeypatch):
    stopped = []
    monkeypatch.setattr(scope, "stop", stopped.append)
    m = match.create()
    out = launcher.remove_match(m.id)
    assert out["status"] == "removed" and stopped == []
    assert match.load(m.id).status == "removed"
    audit = (paths.state_dir() / "audit.log").read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in audit]
    assert kinds == ["match_create", "match_remove"]
    assert "real network" in audit[-1]


def test_match_without_role_is_a_usage_error_before_the_match_is_read(tmp_runtime):
    # QA CR-9: the pairing check comes first, so an unknown match id without --role says
    # "--match and --role go together", not "no match".
    with pytest.raises(HarnessError) as e:
        launcher._validate(LaunchRequest(binary="/bin/true", match="m00000000"))
    assert e.value.code == EXIT_USAGE and "--role" in e.value.message


def test_real_match_accepts_an_agreeing_network_and_a_long_runtime(tmp_runtime):
    # QA TA-7 / CR-12 / MU-7: the 24 h cap is a private-match rule; an explicit network that
    # agrees with the match is fine.
    m = match.create()
    ok = LaunchRequest(binary="/bin/true", match=m.id, role="client", pilot_mode="create",
                       network="real", runtime_sec=match.HOLDER_RUNTIME_SEC + 1)
    launcher._check_network(ok)
    launcher._check_role(ok)


def test_values_with_a_trailing_newline_are_refused(tmp_runtime):
    # Phase B (SA informational, CR): re.match with "$" accepts "x\n"; the values reach argv and
    # multi.cfg, so the checks must cover the whole value.
    m = match.create()
    for bad in (dict(role="host", game_name="g\n", pilot_mode="create"),
                dict(role="client", connect="127.0.0.1:7808\n", pilot_mode="create"),
                dict(role="client", mission="m.fs2\n", pilot_mode="create")):
        with pytest.raises(HarnessError) as e:
            launcher._validate(LaunchRequest(binary="/bin/true", match=m.id, **bad))
        assert e.value.code == EXIT_USAGE, bad


def test_an_expired_real_match_says_so(tmp_runtime):
    m = match.create()
    m.status = "dead"
    match.save(m)
    with pytest.raises(HarnessError) as e:
        match.nsenter_prefix(m)
    assert "day old" in e.value.message


def test_a_launch_in_progress_keeps_an_old_real_match_alive(tmp_runtime, monkeypatch):
    # QA XFA-4: observe runs without the launch lock; a "starting" record whose scope does not
    # exist yet is a launch in progress, not an empty match.
    m = match.create()
    m.created -= match.HOLDER_RUNTIME_SEC + 1
    match.save(m)
    starting = _member(m.id, "client", 7808)          # status "starting", created just now
    assert starting.status == "starting"
    _alive(monkeypatch)                               # its scope is not there yet
    assert match.observe(match.load(m.id)).status == "alive"
    starting.created -= launcher._STARTING_GRACE + 1  # a launch that died long ago
    session.save(starting)
    assert match.observe(match.load(m.id)).status == "dead"
