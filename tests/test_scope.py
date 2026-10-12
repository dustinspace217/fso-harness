import os

import pytest

from fsoctl import scope
from fsoctl.errors import EXIT_LAUNCH, HarnessError


def test_caps_has_all_ceilings_and_floor(monkeypatch):
    monkeypatch.setattr(scope, "_mem_available_kb", lambda: 1_000_000)
    props = scope.caps(runtime_sec=60, cpu_cores=4, tasks=128)
    assert "MemoryMax=2097152K" in props          # floor 2 GiB beats 60% of ~1 GB
    assert "MemorySwapMax=0" in props
    assert "CPUQuota=400%" in props
    assert "TasksMax=128" in props
    assert "RuntimeMaxSec=60" in props


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
