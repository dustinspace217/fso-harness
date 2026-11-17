"""win_scope: Job Objects behind scope.py's public names (cross-platform Plan B, Task 4).

The Win32 calls are replaced by a fake (win_api.kernel32), so the logic that decides limits,
flags, command lines and stop behaviour is tested on every OS. tests/winlive/test_job_live.py
runs the real calls on Windows."""
import ctypes
import subprocess

import pytest

from fsoctl import scope as linux_scope
from fsoctl.errors import EXIT_ENGINE, EXIT_LAUNCH, HarnessError
from fsoctl.platform import win_api, win_scope


REAL_MEM_READER = win_scope._mem_available_kb      # captured before any fixture runs


class FakeKernel:
    """kernel32 as win_scope uses it. Records what it was asked; `fail` names calls that
    return failure; `active` is the list of ActiveProcesses answers (the last one repeats)."""

    def __init__(self, fail=(), active=(1,), exists=False, avail_bytes=64 << 30, jobs=("unit",)):
        self.fail, self.active, self.exists = set(fail), list(active), exists
        self.denied = set()                       # jobs that exist and may not be opened
        self.avail_bytes, self.jobs = avail_bytes, set(jobs)
        self.calls, self.limits, self.cpu, self.closed = [], None, None, []
        self.queried = []                         # the job handle of every count asked for
        self.cmdline = self.environment = self.desktop = self.flags = self.inherit = None
        self.last_error = 0
        # for run(): what each wait on the process answers (the last one repeats), its exit code
        self.wait_results, self.waits, self.exit_code = [win_api.WAIT_OBJECT_0], [], 0

    def _ok(self, name):
        self.calls.append(name)
        return 0 if name in self.fail else 1

    def CreateJobObjectW(self, attributes, name):  # the Win32 names
        self.calls.append("CreateJobObjectW")
        self.job_name = name
        self.last_error = win_api.ERROR_ALREADY_EXISTS if self.exists else 0
        return None if "CreateJobObjectW" in self.fail else 500

    def OpenJobObjectW(self, access, inherit, name):
        self.calls.append("OpenJobObjectW")
        self.access, self.open_inherit = access, inherit
        if name in self.denied:
            self.last_error = win_api.ERROR_ACCESS_DENIED
            return None
        if name not in self.jobs:
            self.last_error = win_api.ERROR_FILE_NOT_FOUND
            return None
        return 600

    def SetInformationJobObject(self, job, info_class, info, size):
        target = info._obj
        if info_class == win_api.JOB_EXTENDED_LIMIT_INFORMATION:
            basic = target.BasicLimitInformation
            self.limits = {"flags": basic.LimitFlags, "processes": basic.ActiveProcessLimit,
                           "memory": target.JobMemoryLimit, "size": size}
        else:
            assert info_class == win_api.JOB_CPU_RATE_CONTROL_INFORMATION
            self.cpu = {"flags": target.ControlFlags, "rate": target.CpuRate}
        return self._ok("SetInformationJobObject")

    def QueryInformationJobObject(self, job, info_class, info, size, returned):
        assert info_class == win_api.JOB_BASIC_ACCOUNTING_INFORMATION
        self.queried.append(job)
        info._obj.ActiveProcesses = self.active.pop(0) if len(self.active) > 1 else self.active[0]
        return self._ok("QueryInformationJobObject")

    def SetHandleInformation(self, handle, mask, flags):
        self.inheritable = (handle, mask, flags)
        return self._ok("SetHandleInformation")

    def CreateProcessW(self, *arguments):
        _app, cmdline, _pattrs, _tattrs, inherit, flags, block, _cwd, si, pi = arguments
        self.cmdline, self.environment, self.flags = cmdline.value, block, flags
        self.inherit = inherit
        self.desktop = si._obj.lpDesktop
        self.std = (si._obj.dwFlags, si._obj.hStdInput, si._obj.hStdOutput, si._obj.hStdError)
        pi._obj.hProcess, pi._obj.hThread, pi._obj.dwProcessId = 700, 701, 4242
        return self._ok("CreateProcessW")

    def InitializeProcThreadAttributeList(self, buffer, count, flags, size):
        self.calls.append("InitializeProcThreadAttributeList")
        assert count == 1
        if buffer is None:                        # the sizing call: say how big, and "fail"
            size._obj.value = 48
            return 0
        return 1

    def UpdateProcThreadAttribute(self, *arguments):
        _buffer, _flags, attribute, handles, size, _previous, _returned = arguments
        assert attribute == win_api.PROC_THREAD_ATTRIBUTE_HANDLE_LIST
        self.handle_list = list(handles)
        assert size == ctypes.sizeof(ctypes.c_void_p) * len(self.handle_list)
        return self._ok("UpdateProcThreadAttribute")

    def DeleteProcThreadAttributeList(self, buffer):
        self.calls.append("DeleteProcThreadAttributeList")

    def AssignProcessToJobObject(self, job, process):
        self.assigned = (job, process)
        return self._ok("AssignProcessToJobObject")

    def ResumeThread(self, thread):
        self.calls.append("ResumeThread")
        self.resumed = thread
        return 0xFFFFFFFF if "ResumeThread" in self.fail else 1

    def TerminateProcess(self, process, code):
        self.terminated_process = process
        return self._ok("TerminateProcess")

    def TerminateJobObject(self, job, code):
        self.terminate_code, self.terminated_job = code, job
        return self._ok("TerminateJobObject")

    def GlobalMemoryStatusEx(self, status):
        status._obj.ullAvailPhys = self.avail_bytes
        return self._ok("GlobalMemoryStatusEx")

    def WaitForSingleObject(self, handle, milliseconds):
        self.calls.append("WaitForSingleObject")
        self.waits.append((handle, milliseconds))
        if len(self.wait_results) > 1:
            return self.wait_results.pop(0)
        return self.wait_results[0]

    def GetExitCodeProcess(self, process, code):
        code._obj.value = self.exit_code
        return self._ok("GetExitCodeProcess")

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


@pytest.fixture
def kernel(monkeypatch):
    """kernel(...) installs a FakeKernel; the log's file handle is the fake number 900."""
    def make(**kw):
        fake = FakeKernel(**kw)
        monkeypatch.setattr(win_api, "kernel32", lambda: fake)
        monkeypatch.setattr(win_api, "last_error", lambda: fake.last_error)
        return fake
    monkeypatch.setattr(win_scope, "_inheritable_handle", lambda f: 900)
    # conftest gives every test a roomy machine by replacing this reader in the scope module
    # the OS uses, which on Windows is the module under test: put the real one back.
    monkeypatch.setattr(win_scope, "_mem_available_kb", REAL_MEM_READER)
    # the desktop itself is win_desktop's business (tests/test_platform_desktop.py)
    monkeypatch.setattr(win_scope.win_desktop, "create", lambda name: 800)
    monkeypatch.setattr(win_scope.win_desktop, "path", lambda name: f"WinSta0\\{name}")
    monkeypatch.setattr(win_scope.win_desktop, "close", lambda handle: None)
    monkeypatch.setattr(win_scope.shutil, "which", lambda name, path=None: name)
    monkeypatch.setattr(win_scope.os, "cpu_count", lambda: 16)
    return make


def start(tmp_path, props=(), argv=("python", "-m", "x"), desktop=None, environment=None):
    props = [*props, f"Desktop={desktop}"] if desktop else list(props)
    return win_scope.start("unit", list(argv), environment or {"PATH": "p"}, tmp_path / "r.log",
                           props)


def test_caps_are_the_same_strings_as_on_linux(kernel, monkeypatch):
    kernel(avail_bytes=64 << 30)
    monkeypatch.setattr(linux_scope, "_mem_available_kb", lambda: 64 * 1048576)
    assert win_scope.caps() == linux_scope.caps()
    assert win_scope.caps(runtime_sec=60, cpu_cores=2, tasks=16, mem_kb=1000) == [
        "MemoryMax=1000K", "MemorySwapMax=0", "CPUQuota=200%", "TasksMax=16", "RuntimeMaxSec=60"]
    assert win_scope.mem_cap_kb() == linux_scope.mem_cap_kb()


def test_room_for_engine_reads_available_physical_memory(kernel):
    kernel(avail_bytes=64 << 30)
    assert win_scope.room_for_engine() is None
    kernel(avail_bytes=9 << 30)                       # 9 GB: 8 reserved leaves under the floor
    assert win_scope.room_for_engine() == "only 9 GB available with 8 GB reserved for the desktop"
    k = kernel(fail=["GlobalMemoryStatusEx"])
    with pytest.raises(HarnessError) as e:
        win_scope.room_for_engine()
    assert e.value.code == EXIT_LAUNCH and k.calls == ["GlobalMemoryStatusEx"]


def test_props_become_job_limits(kernel, tmp_path):
    k = kernel()
    started = start(tmp_path, ["MemoryMax=2097152K", "MemorySwapMax=0", "CPUQuota=800%",
                               "TasksMax=512", "RuntimeMaxSec=1860"])
    assert started.pid == 4242
    assert k.limits["memory"] == 2147483648 and k.limits["processes"] == 512
    assert k.limits["flags"] == (win_api.JOB_OBJECT_LIMIT_JOB_MEMORY
                                 | win_api.JOB_OBJECT_LIMIT_ACTIVE_PROCESS)
    assert k.limits["size"] == ctypes.sizeof(win_api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
    # 800% of one core on a 16-core machine is half of it: 5000 hundredths of a percent, hard cap
    assert k.cpu == {"rate": 5000, "flags": win_api.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                     | win_api.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP}
    assert k.job_name == "unit"


def test_start_never_sets_kill_on_job_close(kernel, tmp_path):
    # the launching command exits while the session runs; that flag would end the session
    k = kernel()
    start(tmp_path, ["MemoryMax=1000K", "TasksMax=4"])
    assert not k.limits["flags"] & win_api.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k = kernel()
    start(tmp_path, [])
    assert k.limits is None and k.cpu is None         # no limits asked: none set


def test_a_cpu_quota_of_the_whole_machine_or_more_sets_no_cap(kernel, tmp_path, monkeypatch):
    k = kernel()
    monkeypatch.setattr(win_scope.os, "cpu_count", lambda: 8)
    start(tmp_path, ["CPUQuota=800%"])
    assert k.cpu is None                              # 8 cores of 8: nothing to cap
    k = kernel()
    start(tmp_path, ["CPUQuota=100%"])
    assert k.cpu["rate"] == 1250


def test_the_job_handle_is_inheritable_so_the_name_outlives_this_process(kernel, tmp_path):
    # probe 10: Windows drops a job's NAME when its last handle closes, even with processes
    # still in it. The child inherits the handle; a later `fsoctl stop` then finds the job.
    k = kernel()
    start(tmp_path)
    assert k.inheritable == (500, win_api.HANDLE_FLAG_INHERIT, win_api.HANDLE_FLAG_INHERIT)
    assert k.inherit == 1
    assert 500 in k.closed                            # this process lets go of its own handle


def test_the_child_inherits_the_log_and_the_job_handle_and_nothing_else(kernel, tmp_path):
    # Found on real Windows (2026-11-16): with plain handle inheritance the session's first
    # process also got the launching command's own output pipe, so whoever was reading that
    # pipe (the MCP server reads `fsoctl launch`'s output) waited until the session ended.
    k = kernel()
    start(tmp_path)
    assert k.handle_list == [900, 500]                # the log, the job
    assert k.calls.count("InitializeProcThreadAttributeList") == 2      # size, then fill
    assert k.calls.index("UpdateProcThreadAttribute") < k.calls.index("CreateProcessW")
    assert k.calls.index("DeleteProcThreadAttributeList") > k.calls.index("CreateProcessW")
    k = kernel(fail=["UpdateProcThreadAttribute"])
    with pytest.raises(HarnessError) as e:
        start(tmp_path)
    assert "UpdateProcThreadAttribute" in e.value.message and "CreateProcessW" not in k.calls
    assert "DeleteProcThreadAttributeList" in k.calls and 500 in k.closed


def test_the_command_line_is_quoted_and_the_child_starts_suspended_in_the_job(kernel, tmp_path):
    k = kernel()
    argv = [r"C:\Program Files\Py\python.exe", "-m", "fsoctl.platform.win_runner", 'a "b" c']
    start(tmp_path, argv=argv, desktop="fso-harness-s0000abcd")
    assert k.cmdline == subprocess.list2cmdline(argv)
    assert k.cmdline.startswith('"C:\\Program Files\\Py\\python.exe" -m')
    assert k.desktop == r"WinSta0\fso-harness-s0000abcd"
    assert k.flags == (win_api.CREATE_SUSPENDED | win_api.CREATE_NEW_PROCESS_GROUP
                       | win_api.CREATE_NO_WINDOW | win_api.CREATE_UNICODE_ENVIRONMENT
                       | win_api.EXTENDED_STARTUPINFO_PRESENT)
    order = [c for c in k.calls if c in ("CreateProcessW", "AssignProcessToJobObject",
                                         "ResumeThread")]
    assert order == ["CreateProcessW", "AssignProcessToJobObject", "ResumeThread"]
    assert k.std[0] == win_api.STARTF_USESTDHANDLES and k.std[2] == k.std[3] == 900
    k = kernel()
    start(tmp_path)
    assert k.desktop is None                          # visible sessions: the user's desktop


def test_the_environment_block_is_sorted_and_double_terminated(kernel, tmp_path):
    k = kernel()
    start(tmp_path, environment={"b": "2", "PATH": "p", "A": "1"})
    assert k.environment[:] == "A=1\0b=2\0PATH=p\0\0"


def test_a_child_that_cannot_join_the_job_or_resume_is_ended(kernel, tmp_path):
    for broken in ("AssignProcessToJobObject", "ResumeThread"):
        k = kernel(fail=[broken])
        with pytest.raises(HarnessError) as e:
            start(tmp_path)
        assert e.value.code == EXIT_LAUNCH and broken in e.value.message
        assert "TerminateProcess" in k.calls and {700, 701, 500} <= set(k.closed)


def test_start_refuses_a_missing_program_and_a_job_name_already_in_use(kernel, tmp_path,
                                                                       monkeypatch):
    k = kernel(exists=True)
    with pytest.raises(HarnessError) as e:
        start(tmp_path)
    assert e.value.code == EXIT_LAUNCH and "already exists" in e.value.message
    assert "CreateProcessW" not in k.calls and 500 in k.closed
    k = kernel()
    monkeypatch.setattr(win_scope.shutil, "which", lambda name, path=None: None)
    with pytest.raises(HarnessError) as e:
        start(tmp_path, argv=["nope.exe"])
    assert e.value.message == "not an executable: nope.exe" and k.calls == []


def test_is_active_needs_an_openable_job_with_a_process_in_it(kernel):
    k = kernel(active=[2])
    assert win_scope.is_active("unit") is True and win_scope.state("unit") == "active"
    assert win_scope.is_gone("unit") is False
    assert k.closed.count(600) == 3                   # every open is closed
    k = kernel(active=[0])
    assert win_scope.is_active("unit") is False and win_scope.state("unit") == "inactive"
    assert win_scope.is_gone("unit") is True
    kernel(jobs=[])                                   # the job cannot be opened: nothing runs
    assert win_scope.is_active("unit") is False and win_scope.is_gone("unit") is True
    # Windows will not say how many processes it holds: it exists, so it is not "gone"
    kernel(fail=["QueryInformationJobObject"])
    assert win_scope.is_active("unit") is True and win_scope.is_gone("unit") is False


def test_stop_ends_the_job_and_waits_for_it_to_empty(kernel, monkeypatch):
    monkeypatch.setattr(win_scope.time, "sleep", lambda s: None)
    k = kernel(active=[3, 3, 1, 0])
    win_scope.stop("unit")
    assert "TerminateJobObject" in k.calls and k.terminate_code == 1
    assert k.access & win_api.JOB_OBJECT_TERMINATE and k.access & win_api.JOB_OBJECT_QUERY
    assert k.closed == [600]
    k = kernel(jobs=[])                               # already gone: nothing to do
    win_scope.stop("unit")
    assert "TerminateJobObject" not in k.calls
    k = kernel(active=[0])                            # empty already
    win_scope.stop("unit")
    assert "TerminateJobObject" not in k.calls


def test_stop_says_so_when_the_job_does_not_empty(kernel, monkeypatch):
    clock = iter(range(0, 1000, 3))
    monkeypatch.setattr(win_scope.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(win_scope.time, "sleep", lambda s: None)
    k = kernel(active=[2])
    with pytest.raises(HarnessError) as e:
        win_scope.stop("unit", timeout=5)
    assert e.value.code == EXIT_ENGINE and "still has 2 process" in e.value.message
    assert k.closed == [600]
    # asked for 5 s, waited the 10 s floor: the clock here moves 3 s a look, so the job was
    # counted once before the end and at 3, 6 and 9 s after it (2 counts without the floor)
    assert len(k.queried) == 4 and win_scope._KILL_WAIT == 10.0


def test_active_units_are_the_live_jobs_of_the_run_folders(kernel, tmp_runtime):
    for name in ("s0000aaaa", "s0000bbbb", "matches", "launch.lock"):
        (tmp_runtime / name).mkdir(parents=True)
    kernel(jobs=["fso-harness-s0000bbbb"])
    assert win_scope.active_units() == ["fso-harness-s0000bbbb.scope"]


def test_the_desktop_is_a_property_only_windows_reads(kernel, tmp_path):
    # the Linux start() keeps its five parameters; the launcher adds the property on Windows
    k = kernel()
    start(tmp_path, ["MemoryMax=1000K"], desktop="fso-harness-s0000abcd")
    assert k.desktop == r"WinSta0\fso-harness-s0000abcd" and k.limits["memory"] == 1024000
    assert k.handle_list == [900, 500, 800]           # the log, the job, the desktop


# ---- run(): the wait-for-it form `fsoctl build` uses (Plan B Task 9) ----
def run(tmp_path, k_props=("MemoryMax=4194304K", "TasksMax=512"), timeout=60.0):
    with open(tmp_path / "build.log", "wb") as log:
        return win_scope.run("unit", ["cmake", "--build", r"C:\my tree\build"], log,
                             list(k_props), timeout)


def test_run_waits_for_the_program_and_returns_its_exit_code(kernel, tmp_path):
    k = kernel()
    k.wait_results = [win_api.WAIT_TIMEOUT, win_api.WAIT_TIMEOUT, win_api.WAIT_OBJECT_0]
    k.exit_code = 1
    assert run(tmp_path) == 1
    assert k.job_name == "unit" and k.cmdline == 'cmake --build "C:\\my tree\\build"'
    assert [handle for handle, _ in k.waits] == [700, 700, 700]
    assert all(0 < ms <= 1000 for _, ms in k.waits)      # short waits: Ctrl-C gets a turn
    assert "TerminateJobObject" not in k.calls
    assert {700, 500} <= set(k.closed)                  # the process and the job are let go


def test_run_makes_a_job_that_dies_with_this_process_and_is_not_handed_on(kernel, tmp_path):
    # MSBuild leaves worker and symbol-server processes behind for minutes after a build.
    # Left in a job that outlives it, they would make the next build of the tree "already
    # running"; and a build whose fsoctl was killed must not keep compiling. So this job ends
    # everything in it when its last handle closes, and only this process holds a handle.
    k = kernel()
    run(tmp_path)
    assert k.limits["flags"] & win_api.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert k.limits["flags"] & win_api.JOB_OBJECT_LIMIT_JOB_MEMORY
    assert k.limits["memory"] == 4194304 * 1024 and k.limits["processes"] == 512
    assert k.handle_list == [900]                       # the log; not the job's handle
    assert k.desktop is None                            # no desktop of its own: nothing draws
    assert k.std[2] == k.std[3] == 900


def test_run_sets_kill_on_close_even_with_no_other_limit(kernel, tmp_path):
    k = kernel()
    run(tmp_path, k_props=())
    assert k.limits["flags"] == win_api.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_run_ends_the_job_and_raises_when_the_time_is_up(kernel, tmp_path, monkeypatch):
    k = kernel()
    k.wait_results = [win_api.WAIT_TIMEOUT]
    clock = iter(range(0, 1000, 20))                    # 20 "seconds" per look at the clock
    monkeypatch.setattr(win_scope.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(subprocess.TimeoutExpired) as e:
        run(tmp_path, timeout=60.0)
    assert e.value.timeout == 60.0
    assert "TerminateJobObject" in k.calls and "GetExitCodeProcess" not in k.calls
    assert {700, 500} <= set(k.closed)


def test_run_refuses_a_missing_program_and_reports_a_start_that_fails(kernel, tmp_path,
                                                                      monkeypatch):
    k = kernel(fail=["CreateProcessW"])
    with pytest.raises(HarnessError) as e:
        run(tmp_path)
    assert e.value.code == EXIT_LAUNCH and "CreateProcessW" in e.value.message
    assert 500 in k.closed                              # the job is not left behind
    k = kernel(fail=["GetExitCodeProcess"])
    with pytest.raises(HarnessError) as e:
        run(tmp_path)
    assert e.value.code == EXIT_LAUNCH and "GetExitCodeProcess" in e.value.message
    monkeypatch.setattr(win_scope.shutil, "which", lambda name, path=None: None)
    k = kernel()
    with pytest.raises(HarnessError) as e:
        run(tmp_path)
    assert e.value.code == EXIT_LAUNCH and "cmake" in e.value.message
    assert "CreateJobObjectW" not in k.calls


# ---- gaps a mutation run found (QA Plan B, MU-19): each test names what it pins ----
GB_KB = 1024 * 1024


def test_the_memory_ceiling_stops_short_of_the_desktops_reserve(kernel):
    # 12 GB free: the 60% share would be 7.2 GB, but only 4 GB lies outside the 8 GB reserve.
    kernel(avail_bytes=12 << 30)
    assert win_scope.mem_cap_kb() == 4 * GB_KB
    # 64 GB free: the share is the smaller of the two.
    kernel(avail_bytes=64 << 30)
    assert win_scope.mem_cap_kb() == int(64 * GB_KB * 0.60)


def test_there_is_room_when_exactly_the_floor_is_left_outside_the_reserve(kernel):
    kernel(avail_bytes=10 << 30)                      # 10 GB - 8 GB reserve = the 2 GB floor
    assert win_scope.room_for_engine() is None
    kernel(avail_bytes=(10 << 30) - 1024)             # one KiB less: no room
    assert "reserved for the desktop" in win_scope.room_for_engine()


def test_the_new_process_goes_into_the_job_just_made_and_its_thread_is_let_go(kernel, tmp_path):
    k = kernel()
    start(tmp_path)
    assert k.assigned == (500, 700) and k.resumed == 701
    assert {701, 700, 500} <= set(k.closed)
    k = kernel()
    run(tmp_path)
    assert k.assigned == (500, 700) and k.resumed == 701
    assert {701, 700, 500} <= set(k.closed)


def test_a_child_that_cannot_join_the_job_is_the_one_that_is_ended(kernel, tmp_path):
    k = kernel(fail=["AssignProcessToJobObject"])
    with pytest.raises(HarnessError):
        start(tmp_path)
    assert k.terminated_process == 700


def test_counting_and_ending_use_the_job_opened_by_name_and_never_an_inheritable_handle(
        kernel, monkeypatch):
    monkeypatch.setattr(win_scope.time, "sleep", lambda s: None)
    k = kernel(active=[2])
    assert win_scope.is_active("unit") is True
    assert k.queried == [600] and k.open_inherit is False
    k = kernel(active=[3, 1, 0])
    win_scope.stop("unit")
    assert k.queried == [600, 600, 600] and k.terminated_job == 600
    assert k.open_inherit is False


def test_run_ends_its_own_job_when_the_time_is_up(kernel, tmp_path, monkeypatch):
    k = kernel()
    k.wait_results = [win_api.WAIT_TIMEOUT]
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(win_scope.time, "monotonic", lambda: float(next(clock)))
    with pytest.raises(subprocess.TimeoutExpired) as e:
        run(tmp_path, timeout=60.0)
    assert k.terminated_job == 500
    assert e.value.cmd == ["cmake", "--build", r"C:\my tree\build"]


def test_start_looks_for_the_program_on_the_sessions_own_path(kernel, tmp_path, monkeypatch):
    seen = {}

    def which(name, path=None):
        seen.update(name=name, path=path)
        return name
    kernel()
    monkeypatch.setattr(win_scope.shutil, "which", which)
    start(tmp_path, argv=["engine.exe", "-window"], environment={"PATH": r"C:\game"})
    assert seen == {"name": "engine.exe", "path": r"C:\game"}


def test_run_checks_the_program_and_not_its_first_argument(kernel, tmp_path, monkeypatch):
    # the build folder exists, the build tool does not: refused before any job is made
    k = kernel()
    monkeypatch.setattr(win_scope.shutil, "which",
                        lambda name, path=None: None if name == "cmake" else name)
    with pytest.raises(HarnessError) as e:
        run(tmp_path)
    assert e.value.message == "not an executable: cmake" and k.calls == []


def test_the_environment_is_sorted_by_name_not_by_value(kernel, tmp_path):
    k = kernel()
    start(tmp_path, environment={"b": "1", "A": "2", "c": "0"})
    assert k.environment[:] == "A=2\0b=1\0c=0\0\0"


def test_a_desktop_that_cannot_be_handed_on_is_closed_and_the_start_refused(kernel, tmp_path,
                                                                            monkeypatch):
    k = kernel()
    closed = []
    monkeypatch.setattr(win_scope.win_desktop, "close", closed.append)
    # the job's handle (500) can be marked inheritable; the desktop's (800) cannot
    k.SetHandleInformation = lambda handle, mask, flags: 0 if handle == 800 else 1
    with pytest.raises(HarnessError) as e:
        start(tmp_path, desktop="fso-harness-s0000abcd")
    assert e.value.code == EXIT_LAUNCH and "SetHandleInformation (desktop)" in e.value.message
    assert closed == [800] and "CreateProcessW" not in k.calls and 500 in k.closed


# ---- QA Plan B, CR-9 / ST-4 / TA-12: "cannot be managed" is not "gone" ----
def test_a_job_this_login_may_not_open_exists_and_is_not_taken_for_gone(kernel):
    # e.g. a session started from an elevated prompt, asked about from an ordinary one:
    # reading that as "gone" let cleanup remove a live session's folder and let `stop`
    # report success while the engine ran on
    k = kernel(jobs=[])
    k.denied = {"unit"}
    assert win_scope.is_active("unit") is True and win_scope.is_gone("unit") is False
    assert win_scope.state("unit") == "active"
    with pytest.raises(HarnessError) as e:
        win_scope.stop("unit")
    assert e.value.code == EXIT_ENGINE and "may not manage" in e.value.message
    assert "TerminateJobObject" not in k.calls and k.closed == []


def test_a_job_that_will_not_be_counted_is_ended_all_the_same(kernel, monkeypatch):
    monkeypatch.setattr(win_scope.time, "sleep", lambda s: None)
    clock = iter(range(0, 1000, 3))
    monkeypatch.setattr(win_scope.time, "monotonic", lambda: next(clock))
    k = kernel(fail=["QueryInformationJobObject"])
    with pytest.raises(HarnessError) as e:
        win_scope.stop("unit", timeout=5)
    # ended, and the wait for it to empty could not be confirmed: said so, not passed over
    assert k.terminated_job == 600 and e.value.code == EXIT_ENGINE
    assert "an unknown number of process(es)" in e.value.message and k.closed == [600]


def test_any_failed_open_but_not_found_means_the_job_may_be_there(kernel):
    # the reviewers' rule: "gone" is a finding (no job of that name), never a failed lookup
    k = kernel(jobs=[])
    k.OpenJobObjectW = lambda access, inherit, name: None       # fails, for some other reason
    k.last_error = 6                                            # "the handle is invalid"
    assert win_scope.is_active("unit") is True
    with pytest.raises(HarnessError) as e:
        win_scope.stop("unit")
    assert e.value.code == EXIT_ENGINE and "Windows error 6" in e.value.message
    k.last_error = win_api.ERROR_FILE_NOT_FOUND
    assert win_scope.is_active("unit") is False
    win_scope.stop("unit")                                      # gone: nothing to do


def test_the_memory_rule_and_the_stop_wait_are_the_linux_numbers():
    # QA Plan B, CR-11: the rule is written out in both modules; the 8 GB reserve is an
    # open question with the owner, and a change must reach both
    for name in ("_MEM_FLOOR_KB", "_MEM_SHARE", "_MEM_RESERVE_KB", "POLITE_STOP_SEC"):
        assert getattr(win_scope, name) == getattr(linux_scope, name), name


def test_an_error_left_over_from_an_earlier_call_is_not_read_as_already_exists(kernel,
                                                                              tmp_path,
                                                                              monkeypatch):
    # QA Plan B, ST-14: "already exists" is read after a call that SUCCEEDED; it is only
    # right if nothing older is still in the error slot
    k = kernel()
    cleared = []
    monkeypatch.setattr(win_api, "clear_last_error", lambda: cleared.append(len(k.calls)))
    start(tmp_path)
    assert cleared == [0]                               # before the job is made, once
