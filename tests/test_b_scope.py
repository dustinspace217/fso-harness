"""scope.stop on a scope that will not die politely (QA SL-4 / CR-1 / SF-4), and scope states.

The first test starts a REAL systemd user scope, like test_scope.py does, but stays in the unit
tier: it never touches the game folder, so it cannot fight other worktrees over the bridge copy.
It is bounded by RuntimeMaxSec=60 and always SIGKILLed in its finally block.
"""
import os
import shutil
import subprocess

import pytest

from fsoctl import scope
from fsoctl.errors import EXIT_ENGINE, EXIT_LAUNCH, HarnessError

def _user_manager_up() -> bool:
    """True if systemd-run exists and a user manager answers (QA TA2-12: a missing user manager
    used to error instead of skip). `is-system-running` prints "offline" when it cannot connect
    and a state word ("running", "degraded", ...) otherwise."""
    if shutil.which("systemd-run") is None:
        return False
    r = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True,
                       text=True, timeout=10, check=False)
    return r.stdout.strip() not in ("", "offline")


needs_systemd = pytest.mark.skipif(not _user_manager_up(), reason="needs a systemd user manager")


@needs_systemd
def test_stop_kills_a_scope_that_ignores_sigterm(tmp_path):
    # `trap "" TERM` makes the shell ignore SIGTERM, and an ignored signal stays ignored across
    # exec, so `sleep` ignores it too: the scope's polite stop can never finish. The user
    # manager's stop timeout here is 45 s (DefaultTimeoutStopUSec), longer than ours.
    unit = f"fso-harness-btest{os.getpid()}"
    proc = None
    try:
        proc = scope.start(unit, ["sh", "-c", 'trap "" TERM; sleep 300'], dict(os.environ),
                           tmp_path / "log", scope.caps(runtime_sec=60))
        scope.stop(unit, timeout=2)
        assert scope.is_gone(unit)
    finally:
        subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", f"{unit}.scope"],
                       capture_output=True, timeout=10, check=False)
        if proc is not None:
            proc.wait(timeout=10)    # reap it: no zombie, no ResourceWarning (QA TA2-12)


@pytest.mark.parametrize("state, gone", [("inactive", True), ("failed", True),
                                         ("unknown", True), ("active", False),
                                         ("deactivating", False), ("activating", False)])
def test_is_gone_counts_only_finished_states(monkeypatch, state, gone):
    # A scope that is still shutting down ("deactivating") still holds the engine, Xvfb and the
    # run folder's files open; cleanup must not treat it as gone (QA SL-2).
    monkeypatch.setattr(scope, "state", lambda unit: state)
    assert scope.is_gone("u") is gone


def test_stop_raises_when_the_scope_survives_sigkill(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(scope, "state", lambda unit: "active")
    monkeypatch.setattr(scope.subprocess, "run", fake_run)
    monkeypatch.setattr(scope, "_KILL_WAIT", 0.2)
    with pytest.raises(HarnessError) as e:
        scope.stop("u", timeout=1)
    assert e.value.code == EXIT_ENGINE
    assert "u" in e.value.message and e.value.hint
    assert any("--signal=SIGKILL" in c for c in calls)


def test_stop_escalates_when_systemctl_stop_times_out(monkeypatch):
    # Python's subprocess timeout must never escape as a traceback (it replaced EXIT_TIMEOUT in
    # launch's error path before this fix).
    states = iter(["active", "deactivating", "inactive"])
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "stop" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(scope, "state", lambda unit: next(states))
    monkeypatch.setattr(scope.subprocess, "run", fake_run)
    scope.stop("u", timeout=1)
    assert any("--signal=SIGKILL" in c for c in calls)


def test_stop_on_a_gone_scope_runs_nothing(monkeypatch):
    monkeypatch.setattr(scope, "state", lambda unit: "inactive")
    monkeypatch.setattr(scope.subprocess, "run", lambda *a, **k: pytest.fail("ran systemctl"))
    scope.stop("u")


def test_active_units_lists_harness_scopes_from_systemd(monkeypatch):
    out = ("fso-harness-s1a2b3c4d.scope loaded active running [systemd-run] /usr/bin/python3\n"
           "fso-harness-s99990000.scope loaded deactivating stop-sigterm [systemd-run] x\n")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(scope.subprocess, "run", fake_run)
    assert scope.active_units() == ["fso-harness-s1a2b3c4d.scope", "fso-harness-s99990000.scope"]
    assert "--state=active,activating,deactivating" in calls[0]


def test_active_units_fails_loudly_when_systemctl_does(monkeypatch):
    monkeypatch.setattr(scope.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout="", stderr="Failed to connect to bus"))
    with pytest.raises(HarnessError) as e:
        scope.active_units()
    assert e.value.code == EXIT_LAUNCH and "bus" in e.value.message and e.value.hint
