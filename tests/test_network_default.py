"""Real network by default (cross-platform Plan A, Task 1; owner's ruling 2026-10-16: "Real
network. I never meant to change this about how the engine works.").

The engine opens its UDP game port on every interface at startup, single-player included
(freespace.cpp game_init -> psnet_init), and when the bind fails it logs and runs with networking
off instead of exiting (psnet2.cpp). So two real-network sessions must never share a port: every
real-network game session gets its own -port, the first free from 7808 up across the host.
"""
import inspect
import json
import re
from pathlib import Path

from fsoctl import cli, launcher, runner, session
from fsoctl.launcher import LaunchRequest


def _sess(**sw):
    """A game session record like test_launcher_unit's, with overrides."""
    base = dict(kind="game", binary="/x/fs2_open", fs2path="/g", mods=["MVPS"], mission="m.fs2",
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[])
    base.update(sw)
    return session.new_session(**base)


def _alive(monkeypatch, *live):
    """Make exactly the given sessions' scopes count as live."""
    names = {s.scope for s in live}
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: unit not in names)


def test_network_defaults_to_real(tmp_runtime):
    assert launcher.resolve_network(LaunchRequest(binary="/bin/true")) == "real"
    assert launcher.resolve_network(LaunchRequest(binary="/bin/true", network="private")) \
        == "private"
    args = cli.build_parser().parse_args(["launch"])
    assert args.network is None            # None = "not given": resolve_network decides


def test_mcp_launch_defaults_to_real():
    # The server imports the mcp package, which the unit tier may not have; read the signature
    # from the source instead of importing it.
    src = (Path(__file__).resolve().parents[1] / "mcp" / "server.py").read_text()
    sig = src[src.index("def launch("):src.index(") -> str:", src.index("def launch("))]
    assert 'network: str | None = None' in sig


def test_two_real_sessions_get_distinct_ports(tmp_runtime, monkeypatch):
    first = _sess(network="real", port=7808)
    _alive(monkeypatch, first)
    assert launcher.host_port(None) == 7809
    _alive(monkeypatch)                       # the first one has stopped
    assert launcher.host_port(None) == 7808


def test_host_port_refuses_a_taken_wanted_port(tmp_runtime, monkeypatch):
    first = _sess(network="real", port=7808)
    _alive(monkeypatch, first)
    try:
        launcher.host_port(7808)
    except launcher.HarnessError as e:
        assert e.code == launcher.EXIT_USAGE
    else:
        raise AssertionError("a taken port was handed out twice")
    assert launcher.host_port(7900) == 7900


def test_private_sessions_do_not_reserve_host_ports(tmp_runtime, monkeypatch):
    # A private session's port lives in its own namespace, not on the host.
    inside = _sess(network="private", port=7808)
    _alive(monkeypatch, inside)
    assert launcher.host_port(None) == 7808


def test_real_session_argv_carries_its_port(tmp_runtime):
    argv = launcher.build_argv(_sess(network="real", port=7810))
    assert argv[argv.index("-port") + 1] == "7810"
    assert "-port" not in launcher.build_argv(_sess(network="private", port=None))


def test_old_record_without_network_runs_private(tmp_runtime, monkeypatch):
    s = _sess()
    data = json.loads((session.run_dir(s) / "session.json").read_text())
    data.pop("network", None)
    (session.run_dir(s) / "session.json").write_text(json.dumps(data))
    old = session.load(s.id)
    monkeypatch.setattr(launcher, "build_argv", lambda sess: ["fs2"])
    assert runner.engine_argv(old) == [*launcher.PRIVATE_NET_PREFIX, "fs2"]


def test_resolve_network_is_documented_in_the_launch_help():
    help_text = cli.build_parser()._subparsers._group_actions[0].choices["launch"].format_help()
    flat = " ".join(help_text.split())
    assert re.search(r"real: [^;]*\(default\)", flat), flat    # QA TA-12: real is the default
    assert "private" in flat
    assert inspect.signature(launcher.resolve_network).return_annotation in (str, "str")
