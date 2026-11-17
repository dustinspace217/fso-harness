import io
import os

import pytest

from fsoctl import scope
from fsoctl.errors import EXIT_LAUNCH, HarnessError

# Taken at import, before conftest's autouse fixture swaps in a fixed 64 GB reader.
_REAL_MEM_AVAILABLE_KB = scope._mem_available_kb


def test_caps_has_all_ceilings_and_floor(monkeypatch):
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 1_000_000)
    props = scope.caps(runtime_sec=60, cpu_cores=4, tasks=128)
    assert "MemoryMax=2097152K" in props          # floor 2 GiB beats 60% of ~1 GB
    assert "MemorySwapMax=0" in props
    assert "CPUQuota=400%" in props
    assert "TasksMax=128" in props
    assert "RuntimeMaxSec=60" in props


def test_mem_cap_never_eats_the_desktop_reserve(monkeypatch):
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 30 * 1024 * 1024)
    assert scope.mem_cap_kb() == 18 * 1024 * 1024          # 60% of 30 GB, reserve not binding
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 12 * 1024 * 1024)
    assert scope.mem_cap_kb() == 4 * 1024 * 1024           # 12 - 8 reserve beats 60% of 12
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 9 * 1024 * 1024)
    assert scope.mem_cap_kb() == scope._MEM_FLOOR_KB        # floor beats 9 - 8


def test_room_for_engine_refuses_a_full_box(monkeypatch):
    refusal = "only 9 GB available with 8 GB reserved for the desktop"
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 9 * 1024 * 1024)
    assert scope.room_for_engine() == refusal
    # The line sits at 10 GiB (8 reserve + 2 floor): exactly that much is room, one KiB less is not.
    edge = 10 * 1024 * 1024
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: edge)
    assert scope.room_for_engine() is None
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: edge - 1)
    assert scope.room_for_engine() == refusal          # 10 GiB less one KiB still prints as 9
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 30 * 1024 * 1024)
    assert scope.room_for_engine() is None


def test_room_for_engine_ignores_swap(monkeypatch):
    """Full swap must not refuse a launch (the swap test was removed 2026-11-11): the guard may
    ask for MemAvailable and nothing else. The real _mem_available_kb runs here (the conftest
    default replaces it), reading through a recording _meminfo_kb; a bare open() from scope fails
    the test, so a swap read that opens /proc/meminfo itself is caught too."""
    asked = []

    def fake_meminfo(*fields):
        asked.extend(fields)
        return {"MemAvailable": 30 * 1024 * 1024, "SwapFree": 0, "SwapTotal": 8 * 1024 * 1024}

    def no_files(*args, **kwargs):
        raise AssertionError(f"scope opened a file: {args}")

    monkeypatch.setattr(scope, "_meminfo_kb", fake_meminfo)
    monkeypatch.setattr(scope, "_mem_available_kb", _REAL_MEM_AVAILABLE_KB)
    monkeypatch.setattr(scope, "open", no_files, raising=False)
    assert scope.room_for_engine() is None
    assert set(asked) == {"MemAvailable"}


def test_meminfo_reads_named_fields_and_names_a_missing_one(monkeypatch):
    text = "MemTotal:       65000000 kB\nMemAvailable:     123456 kB\nSwapFree:              0 kB\n"
    opened = []

    def fake_open(path, encoding=None):
        opened.append(path)
        return io.StringIO(text)

    monkeypatch.setattr(scope, "open", fake_open, raising=False)
    assert scope._meminfo_kb("MemAvailable") == {"MemAvailable": 123456}
    assert _REAL_MEM_AVAILABLE_KB() == 123456
    assert opened == ["/proc/meminfo", "/proc/meminfo"]
    with pytest.raises(HarnessError) as e:
        scope._meminfo_kb("MemAvailable", "NoSuchField", "NorThis")
    assert e.value.code == EXIT_LAUNCH
    assert e.value.message == "cannot read NoSuchField, NorThis from /proc/meminfo"
    assert e.value.hint == "is /proc mounted?"


@pytest.mark.engine
def test_scope_start_is_active_and_stop_is_idempotent(tmp_path):
    unit = f"fso-harness-test{os.getpid()}"
    props = scope.caps(runtime_sec=120)
    proc = scope.start(unit, ["sleep", "60"], dict(os.environ), tmp_path / "log", props)
    # start() must not return before systemd has registered the scope: launch's readiness check
    # treats an inactive scope as a dead session (Task 7 found this race on the real engine).
    assert scope.is_active(unit)
    scope.stop(unit)
    assert not scope.is_active(unit)
    scope.stop(unit)  # second stop must not raise
    proc.wait(timeout=10)  # reap the stopped command (QA TA2-12)


@pytest.mark.engine
def test_scope_start_failure_is_reported(tmp_path):
    unit = f"fso-harness-badtest{os.getpid()}"
    with pytest.raises(HarnessError) as e:
        scope.start(unit, ["/nonexistent/binary"], dict(os.environ), tmp_path / "log",
                    scope.caps(runtime_sec=60))
    assert e.value.code == EXIT_LAUNCH
    assert e.value.message and e.value.hint
