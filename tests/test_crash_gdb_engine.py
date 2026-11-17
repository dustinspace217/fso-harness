"""crash_report and an untrusted binary's embedded gdb script (QA 2026-11-13 AT-1/CR-8, Phase B).

A tiny C program carries an inline Python script in .debug_gdb_scripts (entry type 4) that
writes a canary file when gdb auto-loads it, then aborts. The red proof shows the script is
live: plain gdb with a HOME whose .gdbinit says `set auto-load safe-path /` (what pwndbg does)
runs it. Then: gdb with crash.GDB_SAFETY does not; crash_report on a private-network crash (gdb
runs, with the real ~/.gdbinit and with that fake HOME) does not; crash_report on a sandbox
crash runs no debugger at all. Engine tier (needs FS2PATH for the launch, gcc, gdb, bwrap).
"""
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

from fsoctl import crash, launcher
from fsoctl.launcher import LaunchRequest

pytestmark = [pytest.mark.engine,
              pytest.mark.skipif(not (shutil.which("gcc") and shutil.which("gdb")
                                      and shutil.which("bwrap")),
                                 reason="needs gcc, gdb and bwrap")]

_SOURCE = r'''
#include <stdlib.h>
/* An inline Python script for gdb's auto-load (.debug_gdb_scripts, type 4). */
__asm__(".pushsection \".debug_gdb_scripts\", \"MS\",@progbits,1\n"
        ".byte 4\n"
        ".ascii \"gdb.inlined-script\\n\"\n"
        ".ascii \"open('CANARY', 'w').write('ran')\\n\"\n"
        ".byte 0\n"
        ".popsection\n");
int main(void) { abort(); }
'''


@pytest.fixture
def crasher(tmp_path):
    """Build the crasher; yield (binary, canary path, fake HOME); remove the canary folder."""
    folder = Path("/tmp/claude") / f"gdb-canary-{secrets.token_hex(4)}"
    folder.mkdir(parents=True)
    canary = folder / "ran"
    src = tmp_path / "crasher.c"
    src.write_text(_SOURCE.replace("CANARY", str(canary)))
    exe = tmp_path / "bin" / "crasher"
    exe.parent.mkdir()
    subprocess.run(["gcc", "-g", "-O0", "-o", str(exe), str(src)], check=True, timeout=60)
    home = tmp_path / "fakehome"
    home.mkdir()
    (home / ".gdbinit").write_text("set auto-load safe-path /\n")
    try:
        yield exe, canary, home
    finally:
        shutil.rmtree(folder)


def _gdb(exe: Path, home: Path, extra: str) -> None:
    """Run gdb on `exe` with HOME=home and `extra` arguments (shell-split), then quit."""
    cmd = f"gdb {extra} -batch -ex quit {exe}"
    subprocess.run(["sh", "-c", cmd], env={**os.environ, "HOME": str(home),
                                           "DEBUGINFOD_URLS": ""},
                   capture_output=True, timeout=60, check=False)


def test_red_proof_plain_gdb_runs_the_embedded_script(crasher):
    exe, canary, home = crasher
    _gdb(exe, home, "")
    assert canary.exists(), "the embedded script did not run: the test would prove nothing"


def test_gdb_safety_arguments_stop_the_embedded_script(crasher):
    exe, canary, home = crasher
    _gdb(exe, home, crash.GDB_SAFETY)
    assert not canary.exists()


def _crash(exe: Path, network: str) -> str:
    """Run the crasher as a session on `network` until it aborts; return the session id."""
    sess = launcher.run_to_exit(LaunchRequest(binary=str(exe), network=network,
                                              runtime_sec=60), timeout=60)
    assert sess.exit_info and sess.exit_info.get("signal") == "SIGABRT", sess.exit_info
    return sess.id


def test_crash_report_on_a_private_crash_does_not_run_the_script(engine_env, tmp_runtime,
                                                                 crasher, monkeypatch):
    exe, canary, home = crasher
    sid = _crash(exe, "private")
    first = crash.report(sid)                         # the real ~/.gdbinit
    monkeypatch.setenv("HOME", str(home))             # a .gdbinit that trusts every path
    second = crash.report(sid)
    assert first["core"], "no core dump was found: gdb never ran, so this proves nothing"
    assert first["backtrace"] and second["backtrace"]
    assert not canary.exists()


def test_crash_report_on_a_sandbox_crash_runs_no_debugger(engine_env, tmp_runtime, crasher,
                                                          monkeypatch):
    exe, canary, home = crasher
    sid = _crash(exe, "sandbox")
    monkeypatch.setenv("HOME", str(home))
    out = crash.report(sid)
    assert out["signal"] == "SIGABRT" and out["backtrace"] is None
    assert any(h.startswith("backtrace skipped") for h in out["hints"])
    assert not canary.exists()
