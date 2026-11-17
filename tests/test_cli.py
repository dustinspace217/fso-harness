import json
import os
import signal

import pytest

from fsoctl import cli, platform
from fsoctl.errors import EXIT_NOT_FOUND


def test_version_json(capsys):
    assert cli.main(["--json", "version"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["version"] == "0.1.0"


def test_unknown_session_maps_to_exit_code(capsys, tmp_runtime):
    assert cli.main(["--json", "status", "s00000000"]) == EXIT_NOT_FOUND
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["hint"]


# ---- the stop request on Windows (cross-platform Plan B, Task 10) ----
# Windows has no SIGTERM one process can send another. The MCP server sends Ctrl-Break, which
# Python delivers as signal.SIGBREAK (measured in the VM, 2026-11-16). On Linux these tests
# use SIGUSR1 as a stand-in number for SIGBREAK, which does not exist there.
needs_stand_in = pytest.mark.skipif(not hasattr(signal, "SIGUSR1"),
                                    reason="uses SIGUSR1 as a stand-in for SIGBREAK")


def _record_installs(monkeypatch):
    """Record every signal.signal call (they still take effect); return the list."""
    installed = []
    real = signal.signal

    def record(signum, handler):
        installed.append((signum, handler))
        return real(signum, handler)
    monkeypatch.setattr(signal, "signal", record)
    return installed


@needs_stand_in
def test_on_windows_the_stop_signal_is_ctrl_break(monkeypatch):
    monkeypatch.setattr(signal, "SIGBREAK", signal.SIGUSR1, raising=False)
    installed = _record_installs(monkeypatch)
    monkeypatch.setitem(cli.VERBS, "boom", ("test verb", lambda p: None, lambda args: {}))
    before = signal.getsignal(signal.SIGUSR1)
    assert cli.main(["boom"]) == 0
    assert installed[0] == (signal.SIGUSR1, cli._on_sigterm)
    assert [signum for signum, _ in installed] == [signal.SIGUSR1, signal.SIGUSR1]
    assert signal.getsignal(signal.SIGUSR1) == before        # put back afterwards


@needs_stand_in          # the machines that have SIGUSR1 are the ones with no SIGBREAK
def test_elsewhere_the_stop_signal_is_sigterm(monkeypatch):
    # also when a test has set the platform's name to "windows" on Linux, as many do
    monkeypatch.setattr(platform, "NAME", "windows")
    installed = _record_installs(monkeypatch)
    monkeypatch.setitem(cli.VERBS, "boom", ("test verb", lambda p: None, lambda args: {}))
    assert cli.main(["boom"]) == 0
    assert installed[0] == (signal.SIGTERM, cli._on_sigterm)


@needs_stand_in
def test_ctrl_break_unwinds_so_cleanup_runs_and_a_second_one_is_ignored(monkeypatch):
    monkeypatch.setattr(signal, "SIGBREAK", signal.SIGUSR1, raising=False)
    previous = signal.signal(signal.SIGUSR1, lambda s, f: None)   # a stray one must not kill pytest
    during_cleanup = []

    def verb(args):
        try:
            os.kill(os.getpid(), signal.SIGUSR1)
            for _ in range(100):            # the handler runs at the next bytecode boundary
                pass
            return {}
        finally:
            during_cleanup.append(signal.getsignal(signal.SIGUSR1))
    monkeypatch.setitem(cli.VERBS, "boom", ("test verb", lambda p: None, verb))
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["boom"])
        assert e.value.code == 143 and during_cleanup == [signal.SIG_IGN]
    finally:
        signal.signal(signal.SIGUSR1, previous)
