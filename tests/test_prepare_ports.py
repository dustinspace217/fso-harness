"""_prepare's port wiring and the argv it leads to (plan A QA MU-2..MU-6, TA-2, TA-8).

host_port and build_argv are tested on their own elsewhere; these pin the wiring between them
that a real launch takes: which sessions get a host port, a wanted port through a match, the
recorded `closed`, the whole argv around -port, and the refusal of an unknown match network.
"""
import sys
import pytest

from fsoctl import launcher, match, session
from fsoctl.errors import EXIT_USAGE, HarnessError
from fsoctl.launcher import LaunchRequest

# A stand-in engine binary: the launcher only needs an executable file to exist.
BINARY = sys.executable if sys.platform == "win32" else "/bin/true"


@pytest.fixture
def game(tmp_runtime, monkeypatch):
    """An empty game folder, a no-op bridge install, and every scope counted live."""
    monkeypatch.setattr(launcher.bridge_install, "install", lambda fs2: {"updated": False})
    folder = tmp_runtime.parent / "FS2"
    folder.mkdir()
    monkeypatch.setenv("FS2PATH", str(folder))
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: False)
    return folder


def _prep(**kw):
    sess, _ = launcher._prepare(LaunchRequest(binary=BINARY, **kw))
    return sess


def test_ordinary_real_game_sessions_get_host_ports(game):
    first, second = _prep(), _prep()
    assert (first.network, first.port, second.port) == ("real", 7808, 7809)
    argv = launcher.build_argv(session.load(second.id))
    assert argv[0] == str(launcher.Path(BINARY).resolve())   # the binary survives
    assert argv[argv.index("-port") + 1] == "7809" and "-mod" in argv


@pytest.mark.linux
def test_private_and_qtfred_sessions_get_no_host_port(game):
    assert _prep(network="private").port is None
    assert _prep(kind="qtfred").port is None
    assert _prep().port == 7808                      # neither of them took a host port


@pytest.mark.linux
def test_argv_port_cells(game):
    real_none = _prep()
    real_none.port = None
    assert "-port" not in launcher.build_argv(real_none)         # MU-5: needs network AND port
    private_with_port = _prep(network="private")
    private_with_port.port = 7900
    assert "-port" not in launcher.build_argv(private_with_port)


@pytest.mark.linux
def test_a_wanted_port_and_closed_reach_a_match_session(game):
    m = match.create()
    host = _prep(match=m.id, role="host", pilot_mode="create", port=7900, closed=True)
    assert host.port == 7900 and host.closed is True             # MU-3, MU-4
    assert host.web_port is None and host.web_password is None   # TA-8: a host has no panel
    cfg = (session.prefs_dir(host) / "data" / "multi.cfg").read_text()
    assert "webapi" not in cfg and "webui" not in cfg
    argv = launcher.build_argv(host)
    assert argv[argv.index("-port") + 1] == "7900" and "-startgame" in argv


@pytest.mark.linux
def test_unknown_match_network_is_refused():
    with pytest.raises(HarnessError) as e:
        match.create(network="nope")                             # MU-6: the direct call
    assert e.value.code == EXIT_USAGE and "nope" in e.value.message
