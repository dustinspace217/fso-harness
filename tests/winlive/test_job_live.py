"""win_scope against real Job Objects (plan B Task 4, Step 2)."""
import subprocess
import sys
import time

import pytest

from fsoctl.platform import win_procs, win_scope

# The first process of the job: starts a grandchild, writes both pids, then sleeps.
PARENT = r"""
import os, subprocess, sys, time
kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], close_fds=False)
open(sys.argv[1], "w").write(f"{os.getpid()} {kid.pid}")
time.sleep(120)
"""
# Started by a helper process that exits at once: the session must outlive what started it.
HELPER = r"""
import os, sys
from pathlib import Path
from fsoctl.platform import win_scope
unit, log, pids, parent = sys.argv[1:5]
started = win_scope.start(unit, [sys.executable, "-c", parent, pids], dict(os.environ),
                          Path(log), win_scope.caps(runtime_sec=60, cpu_cores=1, tasks=16,
                                                    mem_kb=1048576))
print(started.pid)
"""


def wait_for(condition, seconds=15.0):
    """Poll `condition` until true or the time is up; return its last value."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:            # bounded
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    return condition()


def test_a_session_outlives_its_starter_and_stop_ends_it_and_its_grandchild(
        tmp_path, job, child_env):
    # plan Review Focus 3: a folder with a space, an apostrophe and non-ASCII letters
    folder = tmp_path / "José Müller's run"
    folder.mkdir()
    pids_file = folder / "pids.txt"
    helper = subprocess.run([sys.executable, "-c", HELPER, job, str(folder / "r.log"),
                             str(pids_file), PARENT], env=child_env, capture_output=True,
                            text=True, timeout=60, check=False)
    assert helper.returncode == 0, helper.stderr
    first = int(helper.stdout.strip())
    assert wait_for(pids_file.exists), "the first process never wrote its pids"
    wait_for(lambda: len(pids_file.read_text().split()) == 2)
    parent, grandchild = (int(p) for p in pids_file.read_text().split())
    assert parent == first
    # the helper has exited; the job is still found by name and both processes are in it
    assert win_scope.is_active(job) and win_scope.state(job) == "active"
    assert win_procs.in_group(parent, job) and win_procs.in_group(grandchild, job)
    assert win_procs.pid_alive(parent) and win_procs.pid_alive(grandchild)
    assert not win_procs.in_group(parent, job + "-other")
    win_scope.stop(job)
    assert wait_for(lambda: win_procs.gone(parent) and win_procs.gone(grandchild))
    assert win_scope.is_gone(job) and not win_scope.is_active(job)
    win_scope.stop(job)                               # stopping again is fine


def test_the_memory_limit_makes_allocation_fail(tmp_path, job, child_env):
    # A job's memory limit does not end the process as Linux's does: allocations fail.
    code = ("try:\n"
            "    blocks = [bytearray(64 << 20) for _ in range(12)]\n"       # 768 MB
            "    print('ALLOCATED', flush=True)\n"
            "except MemoryError:\n"
            "    print('REFUSED', flush=True)\n")
    log = tmp_path / "mem.log"
    started = win_scope.start(job, [sys.executable, "-c", code], child_env, log,
                              ["MemoryMax=200000K"])
    assert wait_for(lambda: win_procs.gone(started.pid), 60)
    text = log.read_text(errors="replace")
    assert "ALLOCATED" not in text, text
    assert "REFUSED" in text or "MemoryError" in text, text


def test_the_process_limit_stops_a_third_process(tmp_path, job, child_env):
    code = ("import subprocess, sys\n"
            "nap = [sys.executable, '-c', 'import time; time.sleep(30)']\n"
            "first = subprocess.Popen(nap)\n"
            "try:\n"
            "    subprocess.Popen(nap)\n"
            "    print('THIRD STARTED', flush=True)\n"
            "except OSError as e:\n"
            "    print('THIRD REFUSED', e.winerror, flush=True)\n"
            "first.kill()\n")
    log = tmp_path / "tasks.log"
    started = win_scope.start(job, [sys.executable, "-c", code], child_env, log, ["TasksMax=2"])
    assert wait_for(lambda: win_procs.gone(started.pid), 60)
    text = log.read_text(errors="replace")
    assert "THIRD REFUSED" in text and "THIRD STARTED" not in text, text


def test_caps_and_room_read_the_real_machine():
    caps = win_scope.caps()
    assert caps[0].startswith("MemoryMax=") and caps[0].endswith("K")
    assert int(caps[0][len("MemoryMax="):-1]) >= 2 * 1024 * 1024
    assert win_scope.room_for_engine() is None or "GB available" in win_scope.room_for_engine()


def test_arguments_with_spaces_quotes_and_accents_arrive_unchanged(tmp_path, job, child_env):
    # plan Review Focus 3: what the child receives is what was passed, however Windows quotes it
    out = tmp_path / "José Müller's run" / "argv.json"
    out.parent.mkdir()
    code = "import json, sys; json.dump(sys.argv[1:], open(sys.argv[1], 'w', encoding='utf-8'))"
    tricky = [str(out), "D:\\My Games\\FS2", 'say "hi"', "Müller,My Mod", "trailing\\", ""]
    started = win_scope.start(job, [sys.executable, "-c", code, *tricky], child_env,
                              tmp_path / "a.log", [])
    assert wait_for(lambda: win_procs.gone(started.pid), 30)
    import json  # noqa: PLC0415
    assert json.loads(out.read_text(encoding="utf-8")) == tricky


# ---- run(): the wait-for-it form `fsoctl build` uses (plan B Task 9) ----
# Says whether its own job can be found by name, leaves a grandchild running as MSBuild
# leaves its workers, and exits with code 3.
LEAVES_ONE_BEHIND = r"""
import subprocess, sys
from fsoctl.platform import win_scope
kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print("grandchild", kid.pid, "found-by-name", win_scope.is_active(sys.argv[1]), flush=True)
sys.exit(3)
"""


def test_run_returns_the_exit_code_and_what_it_left_behind_is_ended(tmp_path, job):
    log_path = tmp_path / "run.log"
    limits = win_scope.caps(runtime_sec=60, cpu_cores=1, tasks=16, mem_kb=1048576)
    with open(log_path, "wb") as log:
        code = win_scope.run(job, [sys.executable, "-c", LEAVES_ONE_BEHIND, job], log, limits, 60)
    assert code == 3
    words = log_path.read_text().split()
    assert words[0] == "grandchild" and words[2:] == ["found-by-name", "True"]
    assert wait_for(lambda: win_procs.gone(int(words[1])), 10)   # ended with the job
    assert win_scope.is_active(job) is False


def test_run_ends_the_whole_tree_when_the_time_is_up(tmp_path, job):
    pids = tmp_path / "pids.txt"
    started = time.monotonic()
    with open(tmp_path / "run.log", "wb") as log, pytest.raises(subprocess.TimeoutExpired):
        win_scope.run(job, [sys.executable, "-c", PARENT, str(pids)], log, [], 5)
    assert time.monotonic() - started < 15
    parent, kid = (int(p) for p in pids.read_text().split())
    assert wait_for(lambda: win_procs.gone(parent) and win_procs.gone(kid), 10)


def test_a_cpu_quota_below_the_whole_machine_is_set_on_the_real_job(tmp_path, job, child_env):
    # QA Plan B, AR-4: the cap is applied only when the quota is less than every processor,
    # which the default of eight cores never is on a small machine, so that branch had not
    # run on Windows once. This reads the limit back from the job; that the limit then holds
    # the processes to it is Windows' part and is not measured here.
    import ctypes  # noqa: PLC0415
    import os  # noqa: PLC0415

    from fsoctl.platform import win_api  # noqa: PLC0415
    cores = os.cpu_count() or 1
    if cores < 2:
        pytest.skip("one processor: a quota of one core is the whole machine")
    win_scope.start(job, [sys.executable, "-c", "import time; time.sleep(60)"], child_env,
                    tmp_path / "c.log", ["CPUQuota=100%"])
    k = win_api.kernel32()
    handle = k.OpenJobObjectW(win_api.JOB_OBJECT_QUERY, False, job)
    assert handle
    try:
        info = win_api.JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        assert k.QueryInformationJobObject(handle, win_api.JOB_CPU_RATE_CONTROL_INFORMATION,
                                           ctypes.byref(info), ctypes.sizeof(info), None)
        assert info.ControlFlags == (win_api.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
                                     | win_api.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP)
        assert info.CpuRate == 100 * 100 // cores       # hundredths of a percent of them all
    finally:
        k.CloseHandle(handle)
