"""Group D fix-wave tests for build (QA SF-11, SL-12, TA-13)."""
import fcntl
import subprocess
from pathlib import Path

import pytest

from fsoctl import build, scope
from fsoctl.errors import EXIT_REFUSED, EXIT_TIMEOUT, HarnessError

DATA = Path(__file__).parent / "data"
# Real failure logs from cmake --build on a two-line project (2026-10-12, this machine):
LINK = (DATA / "build_link.txt").read_text()    # Ninja: undefined reference at link time
MAKE = (DATA / "build_make.txt").read_text()    # Unix Makefiles: the same link failure
CMAKE = (DATA / "build_cmake.txt").read_text()  # CMake re-run fails: a missing source file


def _kinds(diags):
    return [(d["kind"], d["source"]) for d in diags]


def test_errors_come_before_warnings_and_warnings_cannot_push_them_out():
    noise = "".join(f"/w/a.cpp:{i}:1: warning: unused [-Wunused]\n" for i in range(1, 200))
    text = noise + "/w/b.cpp:9:2: error: real problem\n"
    d = build.compiler_errors(text, limit=50, warning_limit=5)
    assert d[0]["message"] == "real problem" and d[0]["kind"] == "error"
    assert [x["kind"] for x in d[1:]] == ["warning"] * 5


def test_linker_error_and_ninja_failure_are_errors():
    d = build.compiler_errors(LINK)
    msgs = " | ".join(x["message"] for x in d)
    assert ("error", "linker") in _kinds(d) and "undefined reference to `foo()'" in msgs
    assert "ld returned 1 exit status" in msgs
    assert ("error", "ninja") in _kinds(d) and "build stopped" in msgs
    # Ninja prints "FAILED: t" BEFORE the linker's lines; the cause must still come first.
    assert d[0]["source"] == "linker" and d[-1]["source"] == "ninja"


def test_make_failure_lines_are_errors():
    d = build.compiler_errors(MAKE)
    assert ("error", "linker") in _kinds(d)
    assert sum(1 for x in d if x["source"] == "make") == 3


def test_cmake_error_keeps_its_explanation():
    d = [x for x in build.compiler_errors(CMAKE) if x["source"] == "cmake"]
    assert len(d) == 2
    assert "Cannot find source file" in d[0]["message"] and "nosuch.cpp" in d[0]["message"]
    assert d[0]["message"].startswith("CMake Error at CMakeLists.txt:3")


# ---- build() with the real build process faked ------------------------------------------------

@pytest.fixture
def tree(tmp_path, tmp_runtime, monkeypatch):
    """A configured-looking worktree; scope.caps / is_active / stop stubbed (no systemd)."""
    wt = tmp_path / "wt"
    (wt / "build").mkdir(parents=True)
    (wt / "build" / "CMakeCache.txt").write_text("")
    monkeypatch.setattr(scope, "caps", lambda **kw: [])
    monkeypatch.setattr(scope, "is_active", lambda unit: False)
    stopped: list[str] = []
    monkeypatch.setattr(scope, "stop", lambda unit, *a, **k: stopped.append(unit))
    return wt, stopped


def _fake_run(monkeypatch, text: str, rc: int, calls: list | None = None):
    """Make subprocess.run write `text` to the build log and return `rc`."""
    def run(cmd, stdout=None, **kw):
        if calls is not None:
            calls.append(cmd)
        stdout.write(text.encode())
        return subprocess.CompletedProcess(cmd, rc)
    monkeypatch.setattr(build.subprocess, "run", run)


def test_failed_build_returns_the_last_40_log_lines(tree, monkeypatch):
    wt, _ = tree
    _fake_run(monkeypatch, "".join(f"line {i}\n" for i in range(100)), 1)
    r = build.build(str(wt))
    assert r["ok"] is False and r["log_tail"] == [f"line {i}" for i in range(60, 100)]
    assert r["oom_suspected"] is False


def test_successful_build_has_no_log_tail(tree, monkeypatch):
    wt, _ = tree
    _fake_run(monkeypatch, "ok\n", 0)
    r = build.build(str(wt))
    assert r["ok"] is True and "log_tail" not in r


@pytest.mark.parametrize("text,rc", [
    ("[1/2] Building\n", -9),
    ("[1/2] Building\n", 137),
    ("c++: fatal error: Killed signal terminated program cc1plus\ncompilation terminated.\n", 1),
])
def test_oom_kill_is_named(tree, monkeypatch, text, rc):
    wt, _ = tree
    _fake_run(monkeypatch, text, rc)
    r = build.build(str(wt))
    assert r["ok"] is False and r["oom_suspected"] is True and "memory" in r["note"]


def test_build_timeout_stops_the_scope(tree, monkeypatch):
    wt, stopped = tree

    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(HarnessError) as e:
        build.build(str(wt), timeout=5)
    assert e.value.code == EXIT_TIMEOUT and e.value.message and e.value.hint
    assert stopped == [build._unit_for(wt.resolve())]


def test_concurrent_build_of_the_same_tree_is_refused(tree, monkeypatch):
    wt, _ = tree
    calls: list = []
    _fake_run(monkeypatch, "", 0, calls)
    with open(wt / "build" / ".fsoctl-build.lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)   # another fsoctl is building this tree
        with pytest.raises(HarnessError) as e:
            build.build(str(wt))
    assert e.value.code == EXIT_REFUSED and e.value.message and e.value.hint
    assert calls == []          # nothing was started, and the other build's log is untouched
    assert build.build(str(wt))["ok"] is True   # the lock is free again afterwards


def test_build_refuses_while_its_scope_is_active(tree, monkeypatch):
    wt, _ = tree
    monkeypatch.setattr(scope, "is_active", lambda unit: True)
    with pytest.raises(HarnessError) as e:
        build.build(str(wt))
    assert e.value.code == EXIT_REFUSED and "systemctl" in e.value.hint


# ---- fix wave 2: R3 cancellation, Y-9 result under the lock ------------------------------------

@pytest.mark.parametrize("exc", [SystemExit(143), KeyboardInterrupt()])
def test_cancelled_build_stops_its_scope_and_reraises(tree, monkeypatch, exc):
    wt, stopped = tree

    def run(cmd, **kw):
        raise exc   # SIGTERM (C8 handler) or Ctrl-C arrives while cmake is running
    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(type(exc)):
        build.build(str(wt))
    assert stopped == [build._unit_for(wt.resolve())]


def test_successful_build_does_not_stop_the_scope(tree, monkeypatch):
    wt, stopped = tree
    _fake_run(monkeypatch, "ok\n", 0)
    build.build(str(wt))
    assert stopped == []


def test_result_is_read_while_the_build_lock_is_held(tree, monkeypatch):
    # Y-9: another build must not truncate the shared log between our run and our read of it.
    wt, _ = tree
    _fake_run(monkeypatch, "ok\n", 0)
    real_result = build._result
    held = []

    def result(rc, log):
        with open(wt / "build" / ".fsoctl-build.lock", "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
            except BlockingIOError:
                held.append(True)
        return real_result(rc, log)
    monkeypatch.setattr(build, "_result", result)
    build.build(str(wt))
    assert held == [True]
