"""The platform package's parts that every system has (cross-platform Plan B, Task 1): the
refusal helper, the lock between processes, the Windows lock and folder logic with its
Windows calls faked, and the path overrides.

Split from tests/test_platform_select.py (QA Plan B, TA-6): that file reloads the package
around the Linux modules and so is collected on Linux only, which had taken these tests,
the only test of the lock between two processes among them, off Windows with it."""
import subprocess
import sys
import time
import types

import pytest

from fsoctl import paths, platform, session
from fsoctl.errors import EXIT_REFUSED, EXIT_TIMEOUT, EXIT_USAGE, HarnessError
from fsoctl.platform import win_dirs, win_locks


def test_each_operating_system_gets_its_name():
    assert platform.name_for("darwin") == "macos"
    assert platform.name_for("win32") == "windows"
    assert platform.name_for("linux") == "linux"


def test_unsupported_names_feature_platform_and_tracker(monkeypatch):
    monkeypatch.setattr(platform, "NAME", "windows")
    with pytest.raises(HarnessError) as e:
        platform.unsupported("--sound", "Plan E")
    assert e.value.code == EXIT_REFUSED == 7
    assert e.value.message == "--sound is not available on windows"
    assert "Plan E" in e.value.hint


def test_needs_linux_passes_on_linux_and_refuses_elsewhere(monkeypatch):
    monkeypatch.setattr(platform, "NAME", "linux")
    assert platform.needs_linux("the keys verb", "Plan C") is None
    monkeypatch.setattr(platform, "NAME", "macos")
    with pytest.raises(HarnessError) as e:
        platform.needs_linux("the keys verb", "Plan C")
    assert e.value.message == "the keys verb is not available on macos"


HOLDER = """
import sys, time
from fsoctl.platform import locks
f = open(sys.argv[1], "a")
assert locks.lock_exclusive(f)
print("held", flush=True)
time.sleep(120)
"""


def test_file_lock_times_out_while_another_process_holds_it(tmp_path):
    # a real second process, on every OS: the lock is between processes, not threads
    lock = tmp_path / "x.lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        start = time.monotonic()
        with pytest.raises(HarnessError) as e, session.file_lock(lock, timeout=0.3):
            raise AssertionError("took a lock another process holds")
        assert e.value.code == EXIT_TIMEOUT and "x.lock" in e.value.message
        assert 0.25 <= time.monotonic() - start < 5
    finally:
        holder.kill()
        holder.wait(timeout=10)
    with session.file_lock(lock, timeout=5):      # the OS dropped it with the holder
        pass


def test_lock_exclusive_says_false_instead_of_waiting(tmp_path):
    lock = tmp_path / "y.lock"
    with open(lock, "a") as first, open(lock, "a") as second:
        assert platform.locks.lock_exclusive(first) is True
        assert platform.locks.lock_exclusive(second) is False
        platform.locks.unlock(first)
        assert platform.locks.lock_exclusive(second) is True


class FakeMsvcrt(types.ModuleType):
    """msvcrt.locking as win_locks uses it: records calls; `busy` makes takes fail."""
    LK_NBLCK, LK_LOCK, LK_UNLCK = 2, 1, 0

    def __init__(self):
        super().__init__("msvcrt")
        self.calls, self.busy = [], 0

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if mode != self.LK_UNLCK and self.busy:
            self.busy -= 1
            raise OSError(13, "Permission denied")


def test_windows_lock_is_one_byte_at_the_start_of_the_file(tmp_path, monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    with open(tmp_path / "z.lock", "a") as f:
        f.write("x")
        assert win_locks.lock_exclusive(f) is True and f.tell() == 0     # byte 0, not the end
        fake.busy = 1
        assert win_locks.lock_exclusive(f) is False                      # held elsewhere
        win_locks.unlock(f)
    assert fake.calls == [(2, 1), (2, 1), (0, 1)]


def test_windows_lock_wait_keeps_trying_and_gives_up_loudly(tmp_path, monkeypatch):
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    with open(tmp_path / "w.lock", "a") as f:
        fake.busy = win_locks.WAIT_TRIES - 1    # msvcrt's LK_LOCK gives up after ~10 s a time
        win_locks.lock_wait(f)                  # the last allowed try gets it
        assert fake.calls == [(1, 1)] * win_locks.WAIT_TRIES
        fake.busy = win_locks.WAIT_TRIES        # never free
        with pytest.raises(OSError):
            win_locks.lock_wait(f)
        assert len(fake.calls) == 2 * win_locks.WAIT_TRIES and win_locks.WAIT_TRIES == 30


def test_windows_roots_come_from_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    base = tmp_path / "Local" / "fso-harness"
    assert win_dirs.runtime_root() == base / "run"
    assert win_dirs.state_root() == base / "state"
    assert win_dirs.sandbox_root() == base / "sandbox"
    monkeypatch.delenv("LOCALAPPDATA")
    with pytest.raises(HarnessError) as e:
        win_dirs.runtime_root()
    assert e.value.code == EXIT_USAGE and "LOCALAPPDATA" in e.value.message


def test_paths_still_puts_the_overrides_first(tmp_path, monkeypatch):
    monkeypatch.setenv("FSO_HARNESS_RUNTIME", str(tmp_path / "rt"))
    monkeypatch.setenv("FSO_HARNESS_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FSO_HARNESS_SANDBOX_ROOT", str(tmp_path / "sb"))
    monkeypatch.setattr(platform.dirs, "runtime_root", lambda: 1 / 0)   # must not be asked
    assert paths.runtime_root() == tmp_path / "rt"
    assert paths.state_dir() == tmp_path / "state"
    assert paths.sandbox_root() == tmp_path / "sb"


def test_paths_asks_the_platform_for_the_defaults(tmp_path, monkeypatch):
    for var in ("FSO_HARNESS_RUNTIME", "FSO_HARNESS_STATE", "FSO_HARNESS_SANDBOX_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(platform.dirs, "runtime_root", lambda: tmp_path / "a")
    monkeypatch.setattr(platform.dirs, "state_root", lambda: tmp_path / "b")
    monkeypatch.setattr(platform.dirs, "sandbox_root", lambda: tmp_path / "c")
    assert paths.runtime_root() == tmp_path / "a" and (tmp_path / "a").is_dir()
    assert paths.state_dir() == tmp_path / "b" and (tmp_path / "b").is_dir()
    assert paths.sandbox_root() == tmp_path / "c" and (tmp_path / "c").is_dir()


def test_the_capability_flags_have_this_systems_values():
    # round-2 TAF-5: tests set these flags; nothing held their REAL values. A flag that was
    # wrong on Windows would switch off the run-folder refusal or the other-login rule with
    # every test still passing, on both systems.
    import sys  # noqa: PLC0415

    from fsoctl import platform  # noqa: PLC0415
    windows = sys.platform == "win32"
    assert platform.BUSY_FILES_REFUSE is windows
    assert platform.GROUPS_PER_LOGIN is windows
    assert platform.PREFS_PATH_ASCII_ONLY is windows
    assert platform.GROUP_TIME_LIMIT is (not windows)
