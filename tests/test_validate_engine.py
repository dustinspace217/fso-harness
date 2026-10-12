import os
import signal
import time
from pathlib import Path

import pytest

from fsoctl import crash, launcher, scratch, session, validate
from fsoctl.launcher import LaunchRequest
from tests.engine_helpers import MODS

pytestmark = pytest.mark.engine


def test_validate_release_build_says_what_it_cannot_see(engine_env, tmp_runtime):
    _, fso = engine_env
    r = validate.validate(MODS, binary=str(fso))
    assert r["aborted"] is False and r["reached"] == "GS_STATE_MAIN_MENU"
    assert r["build"] == "release" and "debug" in r["note"]
    assert not launcher.alive(session.load(r["id"]))


def test_validate_debug_build_reports_a_bad_table(engine_env, tmp_runtime):
    dbg = os.environ.get("FSO_BIN_DEBUG")
    if not dbg:
        pytest.skip("FSO_BIN_DEBUG needed")
    m = scratch.create("validate test")
    try:
        # A ships table that gives the Ulysses a weapon that does not exist: a Warning() at load.
        Path(m["dirs"]["tables"], "zzz-shp.tbm").write_text(
            "#Ship Classes\n$Name: GTF Ulysses\n+nocreate\n"
            '$Default PBanks: ( "No Such Laser" )\n#End\n')
        r = validate.validate([m["name"]], binary=dbg)
    finally:
        scratch.remove(m["name"])
    assert r["build"] == "debug" and r["warnings"] >= 1
    assert any("No Such Laser" in p["text"] for p in r["problems"])


def test_crash_report_after_a_segfault(engine_env, tmp_runtime):
    _, fso = engine_env
    r = launcher.launch(LaunchRequest(binary=str(fso), mods=MODS, skip_movies=True))
    os.kill(session.load(r["id"]).engine_pid, signal.SIGSEGV)
    for _ in range(40):   # wait for the runner to record the exit (bounded, 10 s)
        if session.load(r["id"]).exit_info:
            break
        time.sleep(0.25)
    rep = crash.report(r["id"])
    launcher.stop(r["id"])
    assert rep["signal"] == "SIGSEGV" and rep["status"] == "exited"
    if rep["core"] and rep["core"]["corefile"] == "present":
        assert rep["backtrace"], "a stored core should give a backtrace"
    else:
        assert any("coredumpctl" in h for h in rep["hints"])
