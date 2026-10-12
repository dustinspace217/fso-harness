"""cli.main's error contract: every failure is a structured error with a code, message and hint,
also under --json for argparse errors (QA CR-9 / TA-15), and SIGTERM unwinds (contract C8)."""
import json
import os
import signal
import subprocess

import pytest

from fsoctl import cli
from fsoctl.errors import EXIT_ENGINE, EXIT_FAILED, EXIT_OK, EXIT_TIMEOUT, EXIT_USAGE


def _verb(monkeypatch, fn):
    """Register a throwaway verb "boom" that runs fn()."""
    monkeypatch.setitem(cli.VERBS, "boom", ("test verb", lambda p: None, lambda args: fn()))


def _json_out(capsys) -> dict:
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["message"] and out["hint"]
    return out


def test_argparse_errors_are_json_under_json(capsys):
    assert cli.main(["--json", "launch", "--res"]) == EXIT_USAGE
    out = _json_out(capsys)
    assert out["code"] == EXIT_USAGE and "--res" in out["message"]


def test_unknown_verb_is_a_usage_error_without_json(capsys):
    assert cli.main(["no-such-verb"]) == EXIT_USAGE
    assert "no-such-verb" in capsys.readouterr().err


@pytest.mark.parametrize("exc, code", [
    (OSError(2, "No such file or directory"), EXIT_ENGINE),
    (json.JSONDecodeError("Expecting value", "", 0), EXIT_ENGINE),
    (subprocess.TimeoutExpired(["systemctl"], 10), EXIT_ENGINE),
    (ValueError("invalid literal for int()"), EXIT_USAGE),
])
def test_unexpected_exceptions_become_structured_errors(monkeypatch, capsys, exc, code):
    def raise_it():
        raise exc
    _verb(monkeypatch, raise_it)
    assert cli.main(["--json", "boom"]) == code
    out = _json_out(capsys)
    assert out["code"] == code and type(exc).__name__ in out["message"]


def test_sigterm_unwinds_so_cleanup_runs(monkeypatch):
    # The MCP server ends a timed-out fsoctl with SIGTERM; the verb's `finally: stop` must run.
    # A sentinel handler stands in first, so a missing cli handler fails the test instead of
    # killing pytest.
    caught = []
    previous = signal.signal(signal.SIGTERM, lambda s, f: caught.append(s))
    cleaned = []

    def verb():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(100):     # the handler runs at the next bytecode boundary
                pass
            return {}
        finally:
            cleaned.append(True)
    _verb(monkeypatch, verb)
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["boom"])
        assert e.value.code == 143 and cleaned == [True] and caught == []
        assert signal.getsignal(signal.SIGTERM) is not None
        assert signal.getsignal(signal.SIGTERM) != cli._on_sigterm   # restored afterwards
    finally:
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.parametrize("result, code", [
    ({"ok": False, "returncode": 2, "errors": ["x.cpp:1: error"]}, EXIT_FAILED),
    ({"ok": False, "code": EXIT_TIMEOUT, "message": "build timed out"}, EXIT_TIMEOUT),
    ({"ok": True, "aborted": True}, EXIT_OK),       # validate: a finding, not a failure
    ({"warnings": 0}, EXIT_OK),
])
def test_a_verb_reporting_failure_sets_the_exit_code(monkeypatch, capsys, result, code):
    # Contract C11 (cross-family X-3 / XK-2): a failed build used to exit 0.
    _verb(monkeypatch, lambda: dict(result))
    assert cli.main(["--json", "boom"]) == code
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, **result} if result.get("ok", True) else out == result


def test_a_second_sigterm_cannot_interrupt_the_cleanup(monkeypatch):
    # R3: cleanup after the first SIGTERM (stopping a session) must not itself be cut short.
    previous = signal.signal(signal.SIGTERM, lambda s, f: None)
    during_cleanup = []

    def verb():
        try:
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(100):
                pass
            return {}
        finally:
            during_cleanup.append(signal.getsignal(signal.SIGTERM))
    _verb(monkeypatch, verb)
    try:
        with pytest.raises(SystemExit):
            cli.main(["boom"])
        assert during_cleanup == [signal.SIG_IGN]
    finally:
        signal.signal(signal.SIGTERM, previous)
