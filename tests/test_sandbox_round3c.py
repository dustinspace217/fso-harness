"""Round 3c (QA 2026-11-13 SA4, AT4, CR4, TA4 and the automated review on 8932777): red tests.

Runner-level and bwrap-level tests need bwrap (and Xvfb for the runner); they skip without.
"""
import shutil
import contextlib
import errno
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from fsoctl import launcher, runner, safefs, sandbox, session

_REPO = Path(__file__).resolve().parent.parent
_HAVE_BWRAP = Path("/usr/bin/bwrap").exists()
# The sandbox's command line is prlimit -> keyctl -> bwrap (sandbox.py), so all three must be
# there: a CI runner image without keyutils failed these tests instead of skipping them.
_HAVE_TOOLS = (_HAVE_BWRAP and Path("/usr/bin/Xvfb").exists()
               and all(shutil.which(tool) for tool in ("keyctl", "prlimit")))


def _sbx(tmp_path, **kw) -> session.Session:
    base = dict(kind="game", binary="/bin/true", fs2path=str(tmp_path), mods=[], mission=None,
                renderer="opengl", width=320, height=240, switches={}, allow_eval=False,
                extra_args=[], network="sandbox")
    base.update(kw)
    return session.new_session(**base)


# ---- SA4-1/AT4-2: free space dropped since launch ------------------------------------------------

def test_a_drop_since_launch_over_cap_plus_margin_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "1000")
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_DROP_MARGIN", "2000")
    free = sandbox.free_bytes(tmp_path)
    over = sandbox.Watchdog("s1", tmp_path, None, baseline=free + 3001 + (1 << 20))
    reason = over.check()
    assert reason is not None and "since launch, over the cap plus 2000-byte margin" in reason
    assert sandbox.Watchdog("s1", tmp_path, None, baseline=free).check() is None
    assert sandbox.Watchdog("s1", tmp_path, None, baseline=None).check() is None


@pytest.mark.parametrize("raw, want", [("", 2 << 30), ("x", 2 << 30), ("0", 2 << 30), ("7", 7)])
def test_drop_margin_value(monkeypatch, raw, want):
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_DROP_MARGIN", raw)
    assert sandbox.drop_margin_bytes() == want


_HOLDER = r'''#!/usr/bin/python3
"""Stand-in: writes 64 MiB into a file it unlinked but keeps open, then waits."""
import os, time
prefs = os.environ["FSO_PREFERENCES_PATH"]
hidden = open(os.path.join(prefs, "hidden"), "wb")
os.unlink(os.path.join(prefs, "hidden"))
for i in range(64):
    hidden.write(os.urandom(1 << 20))
    hidden.flush()
    print("wrote", i + 1, "MiB", flush=True)
time.sleep(120)
'''


@pytest.mark.skipif(not _HAVE_TOOLS, reason="needs bwrap, keyctl, prlimit and Xvfb")
def test_an_unlinked_open_file_of_cap_plus_margin_trips_on_the_drop(tmp_runtime, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "fs2_open"
    exe.write_text(_HOLDER)
    exe.chmod(0o755)
    s = _sbx(tmp_path, binary=str(exe))
    env = {**os.environ, "PYTHONPATH": str(_REPO),
           "FSO_HARNESS_SANDBOX_PREFS_MAX": str(8 << 20),        # cap 8 MiB
           "FSO_HARNESS_SANDBOX_DROP_MARGIN": str(16 << 20)}     # + 16 MiB < 64 MiB written
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
    assert final.ended_by == "disk_limit", (final.ended_by, log[-600:])
    assert "since launch, over the cap plus 16777216-byte margin" in log
    written = [int(x.split()[1]) for x in
               (session.run_dir(s) / "engine.log").read_text().splitlines()
               if x.startswith("wrote ")]
    assert written and max(written) > 24, written[-3:]      # the stand-in's bytes crossed it


# ---- automated review: folders vanishing under the walk ---------------------------------

def test_three_walks_that_meet_vanishing_folders_trip(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    walks = []
    real = safefs._listing

    def vanish(base, rel):
        walks.append(rel)
        return None if rel is not None else real(base, rel)
    monkeypatch.setattr(safefs, "_listing", vanish)
    reason = sandbox.Watchdog("s1", tmp_path, None).check()
    assert reason == (f"prefs folder {tmp_path} cannot be measured within bounds: "
                      "folders vanished during 3 walks in a row")
    assert walks.count(None) == 3


def test_one_vanishing_walk_is_retried(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 50_000)
    calls = []
    real = safefs._listing

    def vanish_once(base, rel):
        calls.append(rel)
        if rel == "a" and calls.count("a") == 1:
            return None
        return real(base, rel)
    monkeypatch.setattr(safefs, "_listing", vanish_once)
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "10000")
    reason = sandbox.Watchdog("s1", tmp_path, None).check()
    assert reason is not None and "over the 10000-byte cap" in reason


def test_a_folder_renamed_in_a_loop_never_hides_its_bytes(tmp_path, monkeypatch):
    # A renaming writer against the walk. The file outside the renamed folder is under the cap
    # alone, so only the renamed folder's bytes can push a check over it (QA TA5-1). A listing
    # taken while an entry is renamed can skip it with no error at all, so the retry on
    # vanished folders cannot catch every miss: measured 2026-11-13 on tmpfs, the 3b walk
    # missed 54% of checks and this walk 22%. A missed check only delays the trip to the next
    # one; the hard bound on what renaming can hide is the free-space-drop check (cap + margin).
    (tmp_path / "keep").write_bytes(os.urandom(60_000))
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(os.urandom(60_000))
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_PREFS_MAX", "100000")
    stop, renamed = threading.Event(), [0]

    def rename_loop():
        names = ("a", "b")
        for i in range(2_000_000):                      # bounded; stopped long before
            if stop.is_set():
                return
            try:
                os.rename(tmp_path / names[i % 2], tmp_path / names[(i + 1) % 2])
                renamed[0] += 1
            except OSError:
                continue
    t = threading.Thread(target=rename_loop)
    t.start()
    try:
        results = [sandbox.Watchdog("s1", tmp_path, None).check() for _ in range(200)]
    finally:
        stop.set()
        t.join(10)
    assert renamed[0] > 0                               # the folder really moved during checks
    # the file outside the renamed folder is under the cap; the whole tree is over it
    assert (tmp_path / "keep").stat().st_blocks * 512 < 100_000 < \
        sandbox.safefs.usage(tmp_path, 100)[0]
    caught = sum(r is not None for r in results)
    assert caught >= 120, caught          # at least 60%; the 3b walk caught about 46%


# ---- AT4-1 and SA4-1 under bwrap: io_uring and setxattr ------------------------------------------

_PROBE = textwrap.dedent(r'''
    import ctypes, errno, os, sys
    libc = ctypes.CDLL(None, use_errno=True)
    params = ctypes.create_string_buffer(120)          # struct io_uring_params, zeroed
    fd = libc.syscall(425, 1, params)
    print("io_uring", fd, ctypes.get_errno())
    try:
        os.setxattr("/w/f", "user.fsoh", b"x" * 100)
        print("setxattr ok 0")
    except OSError as e:
        print("setxattr fail", e.errno)
''')


def _probe(tmp_path: Path, with_filter: bool) -> dict:
    """Run _PROBE under bwrap (with or without seccomp_filter); return {name: (result, errno)}."""
    (tmp_path / "f").write_bytes(b"")
    (tmp_path / "probe.py").write_text(_PROBE)
    argv = ["bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
            "--clearenv"]
    r = w = None
    if with_filter:
        r, w = os.pipe()
        os.write(w, sandbox.seccomp_filter())
        os.close(w)
        argv += ["--seccomp", str(r)]
    argv += ["--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
             "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev",
             "--bind", str(tmp_path), "/w", "--", "/usr/bin/python3", "/w/probe.py"]
    out = subprocess.run(argv, pass_fds=(r,) if r is not None else (), capture_output=True,
                         text=True, timeout=30, check=False)
    if r is not None:
        os.close(r)
    rows = [ln.split() for ln in out.stdout.splitlines()]
    return {row[0]: (row[1], int(row[2])) for row in rows}


@pytest.mark.skipif(not _HAVE_BWRAP, reason="needs bwrap")
def test_io_uring_and_setxattr_are_refused_inside(tmp_path):
    got = _probe(tmp_path, with_filter=True)
    assert got["io_uring"] == ("-1", errno.ENOSYS)
    assert got["setxattr"] == ("fail", errno.EPERM)


@pytest.mark.skipif(not _HAVE_BWRAP, reason="needs bwrap")
def test_red_proof_both_work_without_the_filter(tmp_path):
    got = _probe(tmp_path, with_filter=False)
    assert int(got["io_uring"][0]) >= 0, got           # a ring fd: io_uring is enabled here
    assert got["setxattr"] == ("ok", 0), got


# ---- CR4-1: SIGTERM never lands on the watchdog thread ------------------------------------------

_SIGMASK_SCRIPT = textwrap.dedent(r'''
    import os, signal, sys, threading, time
    sys.path.insert(0, sys.argv[1])
    from fsoctl import sandbox
    ran = []
    signal.signal(signal.SIGTERM, lambda *a: ran.append("handler"))
    gate = threading.Event()
    dog = sandbox.Watchdog("s1", sys.argv[2], None)
    sandbox.WATCH_INTERVAL = 0.01
    dog.check = lambda: (gate.wait(5), None)[1]         # hold the thread inside its loop
    dog.start()
    time.sleep(0.2)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})   # as the runner does
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(0.2)
    dog.stop()
    gate.set()
    dog.join(5)
    pending = signal.SIGTERM in signal.sigpending()
    print("handler_ran" if ran else "handler_not_run", "pending" if pending else "not_pending")
''')


def test_sigterm_during_the_join_stays_pending_for_the_main_thread(tmp_path):
    # Without the block in Watchdog.run the kernel can hand SIGTERM to the watchdog thread
    # (the only thread not blocking it), and the handler then runs mid-record.
    script = tmp_path / "sigmask.py"
    script.write_text(_SIGMASK_SCRIPT)
    out = subprocess.run([sys.executable, str(script), str(_REPO), str(tmp_path)],
                         capture_output=True, text=True, timeout=30, check=False)
    assert out.stdout.split() == ["handler_not_run", "pending"], (out.stdout, out.stderr)


# ---- CR4-2: a stop that comes during a check wins ----------------------------------------------

def test_a_stop_during_the_check_does_not_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "WATCH_INTERVAL", 0.01)
    dog = sandbox.Watchdog("s1", tmp_path, None)
    tripped = []

    def check_then_stopped():
        dog.stop()                         # the engine exited while this check ran
        return "too big"
    monkeypatch.setattr(dog, "check", check_then_stopped)
    monkeypatch.setattr(dog, "trip", tripped.append)
    dog.start()
    dog.join(5)
    assert tripped == [] and not dog.is_alive()


# ---- TA4-3: kill() signals bwrap through its pidfd -------------------------------------

def test_kill_goes_through_the_bwrap_pidfd(monkeypatch):
    class Proc:
        returncode = None

        def __init__(self):
            self.calls = []

        def kill(self):
            self.calls.append("kill")

        def wait(self, timeout=None):
            self.returncode = -9
            return -9

        def poll(self):
            return self.returncode
    child = subprocess.Popen(["sleep", "30"])
    proc = Proc()
    eng = sandbox.Engine(proc, None, None, os.pidfd_open(child.pid))
    eng.kill()
    assert child.wait(timeout=5) == -signal.SIGKILL and proc.calls == []
    eng.wait()
    assert eng.bwrap_pidfd is None
    eng.kill()                                       # after the close: Popen's own kill
    assert proc.calls == ["kill"]


def test_a_reaped_bwraps_pidfd_kills_nothing():
    child = subprocess.Popen(["true"])
    fd = os.pidfd_open(child.pid)
    child.wait(timeout=5)
    sandbox.Engine(object(), None, None, fd).kill()   # ProcessLookupError, suppressed
    os.close(fd)
    time.sleep(0)


# ---- automated review on 3c: the bwrap pidfd is opened before anything can reap bwrap ------

def test_the_bwrap_pidfd_is_opened_before_any_poll_or_wait(tmp_runtime, tmp_path, monkeypatch):
    order = []

    class Bwrap:
        pid = os.getpid()

        def __init__(self, argv, **kw):
            order.append("popen")

        def poll(self):
            order.append("poll")

        def wait(self, timeout=None):
            order.append("wait")
            return 0

        def kill(self):
            order.append("kill")
    monkeypatch.setattr(runner.subprocess, "Popen", Bwrap)
    monkeypatch.setattr(runner.sandbox, "_open_pidfd", lambda pid: order.append("pidfd") or 55)
    monkeypatch.setattr(runner.sandbox, "read_info", lambda rfd, timeout: 100)

    def find(init, running, timeout):
        running()                                     # find_engine polls bwrap
        return (101, 99)
    monkeypatch.setattr(runner.sandbox, "find_engine", find)
    monkeypatch.setattr(launcher, "build_argv", lambda sess: ["fs2"])
    eng = runner._popen_engine(_sbx(tmp_path, display=":7"), {"DISPLAY": ":7"}, None)
    assert order[:3] == ["popen", "pidfd", "poll"] and eng.bwrap_pidfd == 55


# ---- CR5-1: a file deleted between the listing and its stat --------------------------------

def test_a_file_deleted_between_listing_and_stat_is_skipped(tmp_path, monkeypatch):
    # The bridge deletes each command file it has claimed. 3b counted such a file as its whole
    # folder vanishing; at the top of prefs (this test) that read as "prefs missing".
    (tmp_path / "cmd-1.json").write_bytes(b"{}")
    (tmp_path / "keep").write_bytes(b"x" * 50_000)
    real = os.scandir

    class Listing:
        """os.scandir stand-in: lists the folder, then deletes cmd-1.json before any stat."""
        def __init__(self, fd):
            with real(fd) as it:
                self.entries = list(it)
            (tmp_path / "cmd-1.json").unlink(missing_ok=True)

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(os, "scandir", Listing)
    size, entries, vanished = safefs.usage(tmp_path, 100)
    assert (entries, vanished) == (1, False)
    assert size == (tmp_path / "keep").stat().st_blocks * 512
    monkeypatch.setattr(os, "scandir", real)
    (tmp_path / "cmd-1.json").write_bytes(b"{}")
    monkeypatch.setattr(os, "scandir", Listing)
    assert sandbox.Watchdog("s1", tmp_path, None).check() is None


# ---- AT5-1: launch warns when another sandbox session is live on the same disk -----------------

def test_launch_warns_about_a_live_sandbox_on_the_same_disk(tmp_runtime, tmp_path, monkeypatch):
    said = []
    monkeypatch.setattr(sandbox, "warn_loudly", said.append)
    other = _sbx(tmp_path)
    session.prefs_dir(other).mkdir(parents=True, exist_ok=True)
    for status, warned in (("running", True), ("exited", False), ("starting", True)):
        said.clear()

        def mark(s, st=status):
            s.status = st
        session.update(other.id, mark)
        launcher._warn_shared_sandbox_disk()
        assert bool(said) is warned, status
        assert not said or other.id in said[0]


def test_a_folder_renamed_between_listing_and_stat_is_a_vanishing_walk(tmp_path, monkeypatch):
    # The automated review on 7b1d2c2: skipping every entry gone before its stat let a folder
    # renamed at that moment drop out of the count with no signal. A vanished folder marks
    # the walk vanished (the watchdog walks again), never "prefs missing".
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 50_000)
    real = os.scandir

    class Listing:
        """os.scandir stand-in: at the top folder, lists, then renames a -> b before any stat."""
        def __init__(self, fd):
            with real(fd) as it:
                self.entries = list(it)
            if (tmp_path / "a").exists() and any(e.name == "a" for e in self.entries):
                (tmp_path / "a").rename(tmp_path / "b")

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(os, "scandir", Listing)
    assert safefs.usage(tmp_path, 100)[2] is True
    (tmp_path / "b").rename(tmp_path / "a")
    walks = []
    real_usage = safefs.usage

    def counting(base, cap):
        walks.append(1)
        if (tmp_path / "b").exists():
            (tmp_path / "b").rename(tmp_path / "a")      # every walk meets the rename again
        return real_usage(base, cap)
    monkeypatch.setattr(safefs, "usage", counting)
    reason = sandbox.Watchdog("s1", tmp_path, None).check()
    assert reason is not None and "folders vanished during 3 walks in a row" in reason
    assert len(walks) == 3
