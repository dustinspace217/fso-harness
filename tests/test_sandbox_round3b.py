"""Round 3b (QA 2026-11-13 automated review, AT3, SA3, CR3, TA3): red tests for each item.

The two runner-level tests start the real runner with a stand-in engine inside real bwrap,
like tests/test_sandbox.py's hostile cells; they need bwrap and Xvfb.
"""
import shutil
import contextlib
import errno
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from fsoctl import channel, crash, launcher, paths, runner, safefs, sandbox, session
from fsoctl.errors import EXIT_REFUSED, HarnessError

_REPO = Path(__file__).resolve().parent.parent
# The sandbox's command line is prlimit -> keyctl -> bwrap (sandbox.py), so all three must be
# there: a CI runner image without keyutils failed these tests instead of skipping them.
_HAVE_TOOLS = (Path("/usr/bin/bwrap").exists() and Path("/usr/bin/Xvfb").exists()
               and all(shutil.which(tool) for tool in ("keyctl", "prlimit")))


def _sbx(tmp_path, **kw) -> session.Session:
    base = dict(kind="game", binary="/bin/true", fs2path=str(tmp_path), mods=[], mission=None,
                renderer="opengl", width=320, height=240, switches={}, allow_eval=False,
                extra_args=[], network="sandbox")
    base.update(kw)
    return session.new_session(**base)


# ---- AT3-1/SA3-1: the watchdog fails closed ----------------------------------------------------

def _filled(prefs: Path, sub: str | None, size: int) -> None:
    """Put `size` bytes into prefs/sub (or prefs itself) as one file."""
    d = prefs / sub if sub else prefs
    d.mkdir(parents=True, exist_ok=True)
    (d / "blob").write_bytes(os.urandom(size))


def test_a_locked_subfolder_still_counts_and_trips(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs"
    _filled(prefs, "x", 200_000)
    os.chmod(prefs / "x", 0)
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "100000")
    reason = sandbox.Watchdog("s1", prefs, None).check()
    assert reason is not None and "over the 100000-byte cap" in reason


def test_a_locked_prefs_folder_still_counts_and_trips(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs"
    _filled(prefs, None, 200_000)
    os.chmod(prefs, 0)
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "100000")
    reason = sandbox.Watchdog("s1", prefs, None).check()
    assert reason is not None and "over the 100000-byte cap" in reason


def test_a_folder_that_cannot_be_restored_trips(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs"
    _filled(prefs, "x", 10)
    os.chmod(prefs / "x", 0)

    def refuse(parent, name):
        raise PermissionError(errno.EPERM, "not allowed")
    monkeypatch.setattr(safefs, "_chmod_private", refuse)
    try:
        reason = sandbox.Watchdog("s1", prefs, None).check()
    finally:
        os.chmod(prefs / "x", 0o700)
    assert reason == (f"prefs folder {prefs} cannot be measured within bounds: "
                      "cannot restore x: not allowed")


def test_an_unreadable_folder_after_restoring_trips(tmp_path, monkeypatch):
    # The engine locks it again between the restore and the read: still a trip.
    prefs = tmp_path / "prefs"
    _filled(prefs, "x", 10)
    os.chmod(prefs / "x", 0)
    monkeypatch.setattr(safefs, "_make_private", lambda base, rel: None)
    try:
        reason = sandbox.Watchdog("s1", prefs, None).check()
    finally:
        os.chmod(prefs / "x", 0o700)
    assert reason is not None and "cannot read x: Permission denied" in reason


def test_any_other_read_error_trips(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs"
    prefs.mkdir()
    real = safefs.folder

    @contextlib.contextmanager
    def io_error(base, rel=None, create=False):
        if rel is None:
            raise OSError(errno.EIO, "disk error")
        with real(base, rel, create) as fd:
            yield fd
    monkeypatch.setattr(safefs, "folder", io_error)
    reason = sandbox.Watchdog("s1", prefs, None).check()
    assert reason == (f"prefs folder {prefs} cannot be measured within bounds: "
                      "cannot read .: disk error")


def test_the_free_space_floor_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_FREE_MIN", str(1 << 62))
    reason = sandbox.Watchdog("s1", tmp_path, None).check()
    assert reason is not None and reason.endswith("-byte floor") and str(1 << 62) in reason


@pytest.mark.parametrize("raw, want", [("", 10 << 30), ("x", 10 << 30), ("0", 10 << 30),
                                       ("5", 5)])
def test_free_floor_value(monkeypatch, raw, want):
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_FREE_MIN", raw)
    assert sandbox.free_min_bytes() == want


def test_launch_refuses_below_the_free_floor(tmp_runtime, tmp_path, monkeypatch):
    binary = tmp_path / "fs2_open"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_FREE_MIN", str(1 << 62))
    with pytest.raises(HarnessError) as e:
        launcher._check_sandbox(launcher.LaunchRequest(binary=str(binary), network="sandbox"))
    assert e.value.code == EXIT_REFUSED and e.value.message.endswith(f"{1 << 62}-byte floor")
    assert "FSO_HARNESS_SANDBOX_ROOT" in e.value.hint


# ---- AT3-4: a locked channel folder is tampering, not "unusable" -------------------------------

def test_a_locked_channel_folder_is_tampering(tmp_runtime, tmp_path):
    s = _sbx(tmp_path)
    ch = channel.Channel(s)
    ch.dir.mkdir(parents=True)
    os.chmod(ch.dir, 0)
    try:
        with pytest.raises(HarnessError) as e:
            ch.send("state", timeout=1)
    finally:
        os.chmod(ch.dir, 0o700)
    assert e.value.details == {"kind": channel.TAMPERED}
    assert e.value.message == "channel tampered: Permission denied"


# ---- CR3-2/CR3-3/SA3-3: cleanup ------------------------------------------------------------------

def _gone(monkeypatch):
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: True)
    monkeypatch.setattr(launcher, "_cleanup_sinks", lambda kept, failed: {})


def test_cleanup_sweeps_sandbox_folders_whose_record_is_gone(tmp_runtime, tmp_path, monkeypatch):
    # CR3-2: after a reboot the /run/user records are gone; the folders on disk must still go.
    root = paths.sandbox_root()
    (root / "s0a1b2c3d" / "prefs" / "x").mkdir(parents=True)
    os.chmod(root / "s0a1b2c3d" / "prefs", 0)
    (root / "not-a-session").mkdir()
    (root / "s11111111").symlink_to(tmp_path)
    _gone(monkeypatch)
    out = launcher.cleanup()
    assert str(root / "s0a1b2c3d") in out["removed_folders"]
    assert not (root / "s0a1b2c3d").exists()
    assert (root / "not-a-session").is_dir() and (root / "s11111111").is_symlink()


def test_the_sweep_keeps_live_young_and_used_folders(tmp_runtime, tmp_path, monkeypatch):
    root = paths.sandbox_root()
    (root / "s22222222").mkdir()
    running = _sbx(tmp_path, status="running")
    monkeypatch.setattr(launcher.scope, "is_gone",
                        lambda unit: unit not in ("fso-harness-s22222222", running.scope))
    monkeypatch.setattr(launcher, "_cleanup_sinks", lambda kept, failed: {})
    out = launcher.cleanup()
    assert (root / "s22222222").is_dir()                  # its scope still exists
    assert session.prefs_dir(running).is_dir()            # used by a kept session
    monkeypatch.setattr(launcher.scope, "is_gone", lambda unit: True)
    out = launcher.cleanup(older_than_sec=3600)            # everything is younger than that
    assert (root / "s22222222").is_dir() and out["removed_folders"] == []


def test_cleanup_uses_the_stored_prefs_root(tmp_runtime, tmp_path, monkeypatch):
    # CR3-3: a session made under another sandbox root is removed where it really is.
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_ROOT", str(tmp_path / "old-root"))
    s = _sbx(tmp_path, status="exited")
    old = session.prefs_dir(s).parent
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_ROOT", str(tmp_path / "new-root"))
    assert tmp_path / "old-root" in launcher._engine_roots()
    _gone(monkeypatch)
    out = launcher.cleanup()
    assert s.id in out["removed"] and not old.exists()


def test_a_replay_from_an_old_root_is_still_strict(tmp_runtime, tmp_path, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_ROOT", str(tmp_path / "old-root"))
    s = _sbx(tmp_path)
    players = session.prefs_dir(s) / "data" / "players"
    players.mkdir(parents=True)
    (players / "X.json").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("{}")
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_ROOT", str(tmp_path / "new-root"))
    with pytest.raises(HarnessError) as e:
        launcher._copy_pilot(players, "X", tmp_path / "dest")
    assert "no pilot files" in e.value.message


# ---- CR3-5: disk_limit survives a later stop; crash_report explains it ---------------------------

def test_disk_limit_survives_a_stop_and_has_a_hint(tmp_runtime, tmp_path, monkeypatch):
    s = _sbx(tmp_path, status="running")
    session.update(s.id, sandbox._mark_disk_limit)
    monkeypatch.setattr(runner, "_oom_kills", lambda: 0)
    runner._record_exit(s.id, -9, "stop")
    rec = session.load(s.id)
    assert rec.ended_by == "disk_limit" and rec.status == "stopped"
    hints = crash._end_hints(rec, "SIGKILL", None)
    assert any(h.startswith("the sandbox's size watchdog ended the run") and
               "another program writing to the same disk" in h for h in hints)          # SA5-1


# ---- TA3-1: terminate() after wait() closed the pidfd -----------------------------------

def test_terminate_after_the_pidfd_was_closed_uses_no_stale_descriptor(monkeypatch):
    calls = []

    class Proc:
        returncode = None

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def terminate(self):
            calls.append("terminate")

        def poll(self):
            return self.returncode
    child = subprocess.Popen(["sleep", "30"])
    try:
        fd = os.pidfd_open(child.pid)
        eng = sandbox.Engine(Proc(), child.pid, fd)
        eng.wait()
        assert eng.pidfd is None and eng.exited()

        def no_signal(*a):
            raise AssertionError("signalled through a closed pidfd")
        monkeypatch.setattr(sandbox.signal, "pidfd_send_signal", no_signal)
        eng.terminate()
        assert calls == ["terminate"] and child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)


# ---- AT3-2 and CR3-1 with bwrap --------------------------------------------------------

_HOLDER = r'''#!/usr/bin/python3
"""Stand-in engine: writes into a file it unlinked but keeps open (no folder walk sees it)."""
import os, time
prefs = os.environ["FSO_PREFERENCES_PATH"]
hidden = open(os.path.join(prefs, "hidden"), "wb")
os.unlink(os.path.join(prefs, "hidden"))
for i in range(192):
    hidden.write(os.urandom(1 << 20))
    hidden.flush()
    print("wrote", i + 1, "MiB", flush=True)   # engine.log is outside prefs: it survives
time.sleep(120)
'''


@pytest.mark.skipif(not _HAVE_TOOLS, reason="needs bwrap, keyctl, prlimit and Xvfb")
def test_unlinked_but_open_bytes_trip_the_free_floor(tmp_runtime, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "fs2_open"
    exe.write_text(_HOLDER)
    exe.chmod(0o755)
    s = _sbx(tmp_path, binary=str(exe))
    free = sandbox.free_bytes(session.prefs_dir(s))
    floor = free - (64 << 20)          # the 192 MiB the stand-in writes cross it
    env = {**os.environ, "PYTHONPATH": str(_REPO), "FSO_HARNESS_SANDBOX_FREE_MIN": str(floor),
           "FSO_HARNESS_SANDBOX_PREFS_MAX": str(1 << 40)}
    with open(session.run_dir(s) / "runner.log", "ab") as runner_log:
        proc = subprocess.Popen([sys.executable, "-m", "fsoctl.runner", s.id], env=env,
                                stdout=runner_log, stderr=runner_log, start_new_session=True)
    try:
        proc.wait(timeout=90)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    final = session.load(s.id)
    log = (session.run_dir(s) / "runner.log").read_text()
    assert final.ended_by == "disk_limit", (final.ended_by, final.exit_info, log[-800:])
    assert "-byte floor; killing the sandboxed engine" in log
    assert list(session.prefs_dir(s).iterdir()) == []      # emptied after the trip
    # TA4-2: the stand-in really wrote past the 64 MiB margin before the trip (another writer
    # on /tmp could otherwise have tripped the floor on its own).
    written = [int(x.split()[1]) for x in
               (session.run_dir(s) / "engine.log").read_text().splitlines()
               if x.startswith("wrote ")]
    assert written and max(written) >= 64, written[-3:]


@pytest.mark.parametrize("args, words", [
    (["-n"], "keep size mode is unsupported"),   # util-linux's wording for EOPNOTSUPP with -n
    ([], "Operation not supported")])
def test_fallocate_fails_with_eopnotsupp_inside(tmp_path, args, words):
    # AT3-2 directly: the filter alone, under bwrap, on a file in a bound folder.
    if not Path("/usr/bin/bwrap").exists():
        pytest.skip("needs bwrap")
    (tmp_path / "reserved").write_bytes(b"")         # fallocate -n needs an existing file
    r, w = os.pipe()
    os.write(w, sandbox.seccomp_filter())
    os.close(w)
    out = subprocess.run(
        ["bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
         "--clearenv", "--seccomp", str(r), "--ro-bind", "/usr", "/usr",
         "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/bin", "/bin",
         "--proc", "/proc", "--dev", "/dev", "--bind", str(tmp_path), "/w", "--",
         "/usr/bin/fallocate", *args, "-l", "64M", "/w/reserved"],
        pass_fds=(r,), capture_output=True, text=True, timeout=20, check=False)
    os.close(r)
    assert out.returncode != 0 and words in out.stderr, out.stderr
    assert (tmp_path / "reserved").stat().st_blocks == 0
