"""The platform package (cross-platform Plan B, Task 1): which module serves each facility on
which OS, and the Linux default folders. What every system has is in test_platform_basics.py.

The package picks once, at import, by sys.platform. Tests of the Windows choice set
sys.platform and reload the package, then reload it back, so no other test sees the change."""
import importlib
import sys

import pytest

import fsoctl.safefs
import fsoctl.scope
from fsoctl import platform
from fsoctl.errors import EXIT_USAGE, HarnessError
from fsoctl.platform import linux_dirs


@pytest.fixture
def as_windows(monkeypatch):
    """The platform package as it loads on Windows; put back as it was afterwards."""
    monkeypatch.setattr(sys, "platform", "win32")
    importlib.reload(platform)
    yield platform
    monkeypatch.undo()                 # sys.platform first, so the reload below picks this OS
    importlib.reload(platform)


def test_linux_selects_the_existing_modules():
    assert platform.NAME == "linux"
    assert platform.scope is fsoctl.scope
    assert platform.safefs is fsoctl.safefs
    assert platform.RUNNER_MODULE == "fsoctl.runner"
    assert platform.locks.__name__ == "fsoctl.platform.linux_locks"
    assert platform.dirs.__name__ == "fsoctl.platform.linux_dirs"


def test_windows_selects_the_win_modules(as_windows):
    assert as_windows.NAME == "windows"
    assert as_windows.scope.__name__ == "fsoctl.platform.win_scope"
    assert as_windows.safefs.__name__ == "fsoctl.platform.win_safefs"
    assert as_windows.locks.__name__ == "fsoctl.platform.win_locks"
    assert as_windows.dirs.__name__ == "fsoctl.platform.win_dirs"
    assert as_windows.procs.__name__ == "fsoctl.platform.win_procs"
    assert as_windows.RUNNER_MODULE == "fsoctl.platform.win_runner"


def test_the_choice_is_put_back_after_a_windows_test():
    # guards the fixture above: a leaked Windows choice would break every later test
    assert platform.NAME == "linux" and platform.scope is fsoctl.scope


def test_linux_default_roots_follow_the_xdg_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "ca"))
    assert linux_dirs.runtime_root() == tmp_path / "run" / "fso-harness"
    assert linux_dirs.state_root() == tmp_path / "st" / "fso-harness"
    assert linux_dirs.sandbox_root() == tmp_path / "ca" / "fso-harness" / "sandbox-runs"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(HarnessError) as e:
        linux_dirs.runtime_root()
    assert e.value.code == EXIT_USAGE and e.value.message == "XDG_RUNTIME_DIR is not set"
