"""Process facts behind the platform layer (cross-platform Plan B, Task 2): is a pid alive,
when did it start, is it in the session's group, who owns a port, which libraries it loaded.

The Windows half runs here with the Win32 calls replaced by fakes (win_api.kernel32 / psapi),
so its logic is tested on every OS; tests/winlive checks the real calls on Windows."""
import ctypes
import os
import signal
import subprocess

import pytest

from fsoctl import launcher, platform, session
from fsoctl.platform import linux_procs, win_api, win_procs
from fsoctl.renderer import classify

NETSTAT_UDP = (
    "\nActive Connections\n\n  Proto  Local Address          Foreign Address        State"
    "           PID\n"
    "  UDP    0.0.0.0:7821           *:*                                    4242\n"
    "  UDP    [::]:7821              *:*                                    4242\n"
    "  UDP    0.0.0.0:78210          *:*                                    9\n")
NETSTAT_TCP = (
    "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       77\n"
    "  TCP    10.0.2.15:8080         10.0.2.2:51000         ESTABLISHED     78\n"
    "  TCP    [::]:8080              [::]:0                 LISTENING       77\n"
    "  TCP    0.0.0.0:18080          0.0.0.0:0              LISTENING       79\n")


def answers(text, seen=None, code=0):
    """A stand-in for subprocess.run that returns `text` and records the command."""
    def run(argv, **kw):
        if seen is not None:
            seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, code, text, "")
    return run


def test_netstat_udp_row_gives_the_owner_pid(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", answers(NETSTAT_UDP, seen))
    monkeypatch.setenv("SYSTEMROOT", r"D:\WinNT")
    assert win_procs.socket_owner_pids("udp", 7821) == {4242}      # 78210 is another port
    assert win_procs.socket_owner_pids("udp", 78210) == {9}
    assert win_procs.socket_owner_pids("udp", 7822) == set()
    argv, kw = seen[0]
    # by its full path in the Windows folder: a bare name would be looked for in the
    # current folder first (QA Plan B, SA-1)
    assert argv == [r"D:\WinNT\System32\netstat.exe", "-ano"]
    assert kw == {"capture_output": True, "text": True, "errors": "replace", "timeout": 10,
                  "check": False}


def test_netstat_tcp_counts_only_listening_rows(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", answers(NETSTAT_TCP, seen))
    assert win_procs.socket_owner_pids("tcp", 8080) == {77}        # 78 is a connection
    assert seen[0][0][0].endswith(r"\System32\netstat.exe") and seen[0][0][1:] == ["-ano"]


def test_the_socket_listing_covers_both_address_families_and_only_the_asked_protocol(
        monkeypatch):
    # Measured in the VM (2026-11-16): `netstat -ano -p UDP` lists IPv4 sockets only, so a
    # port held by an IPv6-only socket looked free; the engine's own socket is IPv6 dual
    # stack and cannot bind such a port. The plain listing has both families, and TCP rows
    # beside the UDP ones.
    listing = (
        "  TCP    0.0.0.0:7852           0.0.0.0:0              LISTENING       55\n"
        "  UDP    0.0.0.0:5353           *:*                                    1\n"
        "  UDP    [::1]:7852             *:*                                    3104\n")
    seen = []
    monkeypatch.setattr(subprocess, "run", answers(listing, seen))
    assert win_procs.socket_owner_pids("udp", 7852) == {3104}      # not the TCP listener
    assert win_procs.socket_owner_pids("tcp", 7852) == {55}
    assert [argv[1:] for argv, _ in seen] == [["-ano"]] * 2      # the whole listing, twice


def test_a_failed_socket_query_owns_nothing(monkeypatch):
    monkeypatch.setattr(subprocess, "run", answers(NETSTAT_UDP, code=1))
    assert win_procs.socket_owner_pids("udp", 7821) == set()
    monkeypatch.setattr(subprocess, "run", answers('x users:(("p",pid=77,fd=3))\n', code=1))
    assert linux_procs.socket_owner_pids("udp", 7808) == set()


def test_ss_rows_give_the_owner_pids_and_the_query_can_run_elsewhere(monkeypatch):
    seen = []
    text = ('UNCONN 0 0 *:7808 *:* users:(("fs2_open",pid=4242,fd=20))\n'
            'UNCONN 0 0 [::]:7808 *:* users:(("fs2_open",pid=4242,fd=21),("x",pid=7,fd=3))\n')
    monkeypatch.setattr(subprocess, "run", answers(text, seen))
    assert linux_procs.socket_owner_pids("udp", 7808) == {4242, 7}
    assert seen[0][0] == ["ss", "-H", "-lunp", "sport = :7808"]
    inside = []

    def run_inside(argv):
        inside.append(argv)
        return subprocess.CompletedProcess(argv, 0, 'x users:(("p",pid=5,fd=3))\n', "")
    assert linux_procs.socket_owner_pids("tcp", 8080, run_inside) == {5}
    assert inside == [["ss", "-H", "-ltnp", "sport = :8080"]] and len(seen) == 1


class FakeKernel:
    """kernel32 as win_procs uses it. Handles are small numbers; `wait` is what
    WaitForSingleObject answers for a process handle."""

    def __init__(self, processes=(), wait=win_api.WAIT_TIMEOUT, jobs=(), in_job=True,
                 created=(0x11, 0x22)):
        self.processes, self.wait, self.jobs = set(processes), wait, set(jobs)
        self.in_job, self.created = in_job, created
        self.opened, self.closed, self.access = [], [], []
        self.inherit = []                         # the inherit flag of every open
        self.denied, self.last_error = set(), 0   # pids that exist and may not be opened

    def OpenProcess(self, access, inherit, pid):  # the Win32 names
        self.access.append(access)
        self.inherit.append(inherit)
        if pid in self.denied:
            self.last_error = win_api.ERROR_ACCESS_DENIED
            return None
        if pid not in self.processes:
            self.last_error = win_api.ERROR_INVALID_PARAMETER    # what Windows says: no such pid
            return None
        self.opened.append(1000 + pid)
        return 1000 + pid

    def WaitForSingleObject(self, handle, ms):
        assert ms == 0
        return self.wait

    def GetProcessTimes(self, handle, creation, exited, kernel, user):
        creation._obj.low, creation._obj.high = self.created
        return 1

    def OpenJobObjectW(self, access, inherit, name):
        self.inherit.append(inherit)
        if name not in self.jobs:
            return None
        self.opened.append(name)
        return name

    def IsProcessInJob(self, process, job, result):
        result._obj.value = 1 if self.in_job else 0
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


@pytest.fixture
def kernel(monkeypatch):
    """Install a FakeKernel factory: kernel(...) builds one and makes win_api return it."""
    def make(**kw):
        fake = FakeKernel(**kw)
        monkeypatch.setattr(win_api, "kernel32", lambda: fake)
        return fake
    return make


def test_pid_alive_true_while_the_process_has_not_ended(kernel):
    k = kernel(processes=[10])
    assert win_procs.pid_alive(10) is True
    assert k.access == [win_api.SYNCHRONIZE | win_api.PROCESS_QUERY_LIMITED_INFORMATION]
    assert k.closed == k.opened == [1010]              # every handle opened is closed


def test_pid_alive_false_for_an_exited_process_handle_still_open(kernel):
    # another program may still hold a handle to an ended process: it opens, but it has ended
    k = kernel(processes=[10], wait=win_api.WAIT_OBJECT_0)
    assert win_procs.pid_alive(10) is False and win_procs.gone(10) is True
    assert k.closed == k.opened


def test_pid_alive_false_when_there_is_no_such_process(kernel):
    k = kernel()
    assert win_procs.pid_alive(10) is False and win_procs.gone(10) is True
    assert k.closed == []


def test_windows_pid_start_is_the_creation_time_as_one_number(kernel):
    k = kernel(processes=[10], created=(0xDDCCBBAA, 0x01DA0000))
    assert win_procs.pid_start(10) == (0x01DA0000 << 32) | 0xDDCCBBAA
    assert k.closed == k.opened
    assert win_procs.pid_start(11) is None             # gone


def test_windows_in_group_asks_whether_the_process_is_in_the_named_job(kernel):
    k = kernel(processes=[10], jobs=["fso-harness-sab12"])
    assert win_procs.in_group(10, "fso-harness-sab12") is True
    assert sorted(map(str, k.closed)) == sorted(map(str, k.opened)) and len(k.opened) == 2
    assert win_procs.in_group(10, "fso-harness-other") is False      # no such job
    assert win_procs.in_group(11, "fso-harness-sab12") is False      # no such process
    k = kernel(processes=[10], jobs=["fso-harness-sab12"], in_job=False)
    assert win_procs.in_group(10, "fso-harness-sab12") is False      # a reused pid elsewhere
    assert len(k.closed) == 2


class StillRunning:
    """An engine stand-in that never exits by itself."""
    pid = 1

    def poll(self):
        return None


def test_windows_ask_stop_leaves_the_stop_marker_the_runner_watches(tmp_runtime, monkeypatch):
    # One file, one name (QA Plan B CR-6, ST-14, TA-9): the marker `fsoctl stop` has always
    # left in the run folder is the request, and the Windows runner watches for that file.
    from fsoctl.platform import win_runner  # noqa: PLC0415
    sess = _session()
    assert win_procs.ask_stop(sess) is None
    marker = session.run_dir(sess) / session.STOP_MARKER
    assert marker.is_file()
    assert win_procs.ask_stop(sess) is None            # asking twice is fine
    assert sorted(p.name for p in session.run_dir(sess).iterdir() if "stop" in p.name) == [
        session.STOP_MARKER]                           # and no second file beside it
    # the runner's wait takes that file as the stop request
    monkeypatch.setattr(win_runner, "_end_engine", lambda engine: 0)
    assert win_runner.wait_for_end(StillRunning(), marker, 60, 0.0) == ("stop", 0)


def test_windows_ask_stop_does_not_write_a_marker_that_is_already_there(tmp_runtime,
                                                                        monkeypatch):
    # round-2 CRF-5: stop() has made the marker. A second write that failed (a busy file)
    # answered "skipped", and the group was then ended by force under a runner that was
    # already closing the engine.
    sess = _session()
    (session.run_dir(sess) / session.STOP_MARKER).touch()

    def refuse(self, *arguments, **keywords):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(type(session.run_dir(sess)), "touch", refuse)
    assert win_procs.ask_stop(sess) is None


def test_windows_ask_stop_that_cannot_leave_the_marker_says_so_and_does_not_raise(
        tmp_runtime, monkeypatch):
    # the Linux twin returns "skipped:..." when it cannot signal; a raise here kept stop()
    # from ever reaching the process group's own stop
    sess = _session()

    def refuse(self, *arguments, **keywords):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(type(session.run_dir(sess)), "touch", refuse)
    assert win_procs.ask_stop(sess) == "skipped:Permission denied"


def test_stop_waits_longer_for_the_windows_runner_than_the_runner_gives_the_engine():
    from fsoctl.platform import win_runner  # noqa: PLC0415
    assert launcher._RUNNER_EXIT_WAIT > win_runner.RUNNER_POLL_S + win_runner.ENGINE_CLOSE_WAIT_S


def test_linux_ask_stop_signals_the_runner_and_reports_a_refusal(monkeypatch, tmp_runtime):
    sess = _session()
    sess.runner_pid = 4242
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert linux_procs.ask_stop(sess) is None and sent == [(4242, signal.SIGTERM)]

    def deny(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "kill", deny)
    assert linux_procs.ask_stop(sess) == "skipped:Operation not permitted"


class FakePsapi:
    """psapi as loaded_libraries uses it: `names` are the module file names, in order."""

    def __init__(self, names, enum_ok=1):
        self.names, self.enum_ok = names, enum_ok

    def EnumProcessModulesEx(self, handle, modules, size, needed, which):
        assert which == win_api.LIST_MODULES_ALL
        for i in range(len(self.names)):
            modules[i] = i + 1
        needed._obj.value = len(self.names) * ctypes.sizeof(ctypes.c_void_p)
        return self.enum_ok

    def GetModuleFileNameExW(self, handle, module, buffer, size):
        buffer.value = self.names[module - 1]
        return len(buffer.value)


def test_windows_loaded_libraries_lists_the_module_files(kernel, monkeypatch):
    names = [r"C:\FS2\fs2_open.exe", r"C:\FS2\libgallium_wgl.dll", r"C:\Windows\System32\x.dll"]
    k = kernel(processes=[10])
    monkeypatch.setattr(win_api, "psapi", lambda: FakePsapi(names))
    assert win_procs.loaded_libraries(10) == "\n".join(names)
    assert k.access == [win_api.PROCESS_QUERY_INFORMATION | win_api.PROCESS_VM_READ]
    assert k.closed == k.opened
    assert win_procs.loaded_libraries(11) == ""        # gone: nothing, as on Linux
    monkeypatch.setattr(win_api, "psapi", lambda: FakePsapi(names, enum_ok=0))
    assert win_procs.loaded_libraries(10) == ""


# Only the Mesa rows (libgallium_wgl.dll) were seen in a real run (the VM, 2026-11-16). The
# vendors' driver names and the place of vulkan-1.dll are what the code expects, not what
# was measured: no run on a machine with a graphics card exists yet (renderer.py says so).
# These rows pin the code's reading so a change is deliberate; they are not evidence.
@pytest.mark.parametrize("libraries, renderer", [
    ([r"C:\Windows\System32\DriverStore\nv_dispi.inf_amd64\nvoglv64.dll"], "opengl"),
    ([r"C:\Windows\System32\atio6axx.dll"], "opengl"),
    ([r"C:\Windows\System32\DriverStore\iigd_dch.inf\ig9icd64.dll"], "opengl"),
    ([r"C:\FS2\opengl32.dll", r"C:\FS2\libgallium_wgl.dll"], "opengl"),
    ([r"C:\Windows\System32\vulkan-1.dll", r"C:\x\nvoglv64.dll"], "vulkan"),
    # Mesa's software OpenGL loaded beside the Vulkan loader: OpenGL, as on Linux
    ([r"C:\Windows\System32\vulkan-1.dll", r"C:\FS2\libgallium_wgl.dll"], "opengl"),
    ([r"C:\Windows\System32\kernel32.dll"], "unknown"),
])
def test_classify_reads_windows_library_names(libraries, renderer):
    found = classify("\n".join(libraries))
    assert found["renderer"] == renderer
    known = {"nvoglv64.dll", "atio6axx.dll", "ig9icd64.dll", "libgallium_wgl.dll",
             "vulkan-1.dll"}
    assert set(found["libraries"]) == {p.rsplit("\\", 1)[1] for p in libraries} & known


def _session(**kw):
    base = dict(kind="game", binary="/bin/true", fs2path="/tmp", mods=[], mission=None,
                renderer="opengl", width=1280, height=720, switches={}, allow_eval=False,
                extra_args=[])
    base.update(kw)
    return session.new_session(**base)


def _running(monkeypatch, runner_start):
    """A running session with runner pid 10, its scope stubbed, and procs answering that pid 10
    is in the scope; returns (session, the list ask_stop appends to)."""
    s = _session()

    def mark(rec):
        rec.status, rec.runner_pid, rec.runner_start = "running", 10, runner_start
    session.update(s.id, mark)
    monkeypatch.setattr(launcher.scope, "stop", lambda unit, timeout=20: None)
    monkeypatch.setattr(launcher.procs, "in_group", lambda pid, unit: True)
    monkeypatch.setattr(launcher.procs, "gone", lambda pid: True)
    asked = []
    monkeypatch.setattr(launcher.procs, "ask_stop", lambda sess: asked.append(sess.id))
    return s, asked


def test_a_reused_pid_is_not_the_runner(tmp_runtime, monkeypatch):
    # plan Review Focus 2: the recorded pid now belongs to a process that started later
    s, asked = _running(monkeypatch, runner_start=111)
    monkeypatch.setattr(launcher.procs, "pid_start", lambda pid: 222)
    r = launcher.stop(s.id)
    assert r["runner_first"] == "skipped:runner not running in the session's scope"
    assert asked == [] and r["status"] == "stopped"


def test_the_runner_with_the_recorded_start_is_asked_to_stop(tmp_runtime, monkeypatch):
    s, asked = _running(monkeypatch, runner_start=111)
    monkeypatch.setattr(launcher.procs, "pid_start", {10: 111}.get)   # pid 10 only
    assert launcher.stop(s.id)["runner_first"] == "ok" and asked == [s.id]


def test_a_record_without_a_start_time_falls_back_to_the_group_check(tmp_runtime, monkeypatch):
    # records written before this field existed, and every Linux runner's
    s, asked = _running(monkeypatch, runner_start=None)
    monkeypatch.setattr(launcher.procs, "pid_start", lambda pid: 1 / 0)    # must not be asked
    assert launcher.stop(s.id)["runner_first"] == "ok" and asked == [s.id]


def test_an_old_session_record_loads_without_a_runner_start(tmp_runtime):
    s = _session()
    record = session.run_dir(s) / "session.json"
    text = record.read_text()
    assert '"runner_start"' in text
    record.write_text("\n".join(ln for ln in text.splitlines() if '"runner_start"' not in ln))
    assert session.load(s.id).runner_start is None


def test_alive_asks_the_platform_about_the_engine_pid(tmp_runtime, monkeypatch):
    s = _session()
    s.engine_pid = 4242
    monkeypatch.setattr(launcher.scope, "is_active", lambda unit: True)
    monkeypatch.setattr(launcher.procs, "pid_alive", lambda pid: pid == 4242)
    assert launcher.alive(s) is True
    s.engine_pid = 4243
    assert launcher.alive(s) is False


@pytest.mark.linux
def test_the_platform_names_the_procs_module_for_this_os():
    assert platform.procs is linux_procs
    assert linux_procs.pid_alive(os.getpid()) is True
    assert linux_procs.pid_alive(2**22 + 12345) is False


# ---- gaps a mutation run found (QA Plan B, MU-10, MU-23): each test names what it pins ----
@pytest.mark.linux
def test_linux_loaded_libraries_is_the_process_map_and_empty_for_no_such_process():
    text = linux_procs.loaded_libraries(os.getpid())
    assert "libc" in text and text.count("\n") > 5       # this process maps the C library
    assert linux_procs.loaded_libraries(2 ** 22 + 12345) == ""     # above the largest pid


def test_a_connection_row_listed_first_does_not_hide_the_listener(monkeypatch):
    rows = ("  TCP    10.0.2.15:8080         10.0.2.2:51000         ESTABLISHED     78\n"
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       77\n")
    monkeypatch.setattr(subprocess, "run", answers(rows))
    assert win_procs.socket_owner_pids("tcp", 8080) == {77}


def test_no_handle_the_lookups_open_is_one_a_child_process_inherits(kernel):
    k = kernel(processes=[10], jobs=["unit"])
    win_procs.pid_alive(10)
    win_procs.pid_start(10)
    win_procs.in_group(10, "unit")
    assert k.inherit and all(flag is False for flag in k.inherit)


# ---- QA Plan B, CR-1 / ST-3: "known to have ended" is not "could not be asked" ----
def test_windows_pid_ended_is_true_only_when_windows_says_so(kernel, monkeypatch):
    k = kernel(processes=[10])
    monkeypatch.setattr(win_api, "last_error", lambda: k.last_error)
    assert win_procs.pid_ended(10) is False                 # running
    assert win_procs.pid_ended(11) is True                  # no process has this pid
    k.denied = {12}
    assert win_procs.pid_ended(12) is False                 # exists; not ours to ask: not known
    k = kernel(processes=[10], wait=win_api.WAIT_OBJECT_0)
    monkeypatch.setattr(win_api, "last_error", lambda: k.last_error)
    assert win_procs.pid_ended(10) is True                  # ended, a handle still open
    k = kernel(processes=[10], wait=0xFFFFFFFF)             # the wait itself failed
    monkeypatch.setattr(win_api, "last_error", lambda: k.last_error)
    assert win_procs.pid_ended(10) is False
    assert k.closed == k.opened and k.inherit == [False]


@pytest.mark.linux
def test_linux_pid_ended_is_true_only_for_a_pid_no_process_has(monkeypatch):
    assert linux_procs.pid_ended(os.getpid()) is False
    assert linux_procs.pid_ended(2 ** 22 + 12345) is True   # above the largest pid

    def not_ours(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "kill", not_ours)
    assert linux_procs.pid_ended(1) is False                # it exists; not ours to signal


def test_windows_lock_exclusive_says_held_only_for_the_held_error(monkeypatch, tmp_path):
    # QA Plan B, CR-21 / ST-14: every error read as "held" turned a real fault into a
    # 60 second lock time-out
    import errno  # noqa: PLC0415
    import types  # noqa: PLC0415

    from fsoctl.platform import win_locks  # noqa: PLC0415
    answers_ = []

    def locking(fd, mode, count):
        if answers_:
            raise answers_.pop(0)
    fake = types.SimpleNamespace(locking=locking, LK_NBLCK=2, LK_LOCK=1, LK_UNLCK=0)
    monkeypatch.setitem(__import__("sys").modules, "msvcrt", fake)
    with open(tmp_path / "x.lock", "a", encoding="utf-8") as f:
        assert win_locks.lock_exclusive(f) is True
        answers_.append(PermissionError(errno.EACCES, "Permission denied"))
        assert win_locks.lock_exclusive(f) is False
        answers_.append(OSError(errno.EDEADLK, "Resource deadlock avoided"))
        assert win_locks.lock_exclusive(f) is False
        answers_.append(OSError(errno.EBADF, "Bad file descriptor"))
        with pytest.raises(OSError, match="Bad file descriptor"):
            win_locks.lock_exclusive(f)
