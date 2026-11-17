"""OS-level tests of the Windows platform modules (cross-platform Plan B): the real Win32
calls, on Windows only. Marked `winlive` and left out of the default run (pytest.ini), so the
unit tier never starts real processes; CI's Windows job and the local VM run them with
`pytest -m winlive tests/winlive`.

Every test here starts processes only inside a Job Object it ends itself (the `job` fixture),
so a failed test cannot leave anything running."""
import os
import secrets
import sys

import pytest


def pytest_collection_modifyitems(items):
    """Mark everything in this folder `winlive`, and skip it anywhere but Windows."""
    for item in items:
        if "winlive" in str(item.fspath):
            item.add_marker(pytest.mark.winlive)
            if sys.platform != "win32":
                item.add_marker(pytest.mark.skip(reason="needs Windows"))


@pytest.fixture
def job():
    """A fresh job name; whatever is still in that job is ended after the test."""
    from fsoctl.platform import win_scope  # noqa: PLC0415 - Windows-only at call time
    name = f"fso-harness-test-{secrets.token_hex(4)}"
    yield name
    win_scope.stop(name, timeout=10)


@pytest.fixture
def child_env():
    """The environment for started processes: this one's, plus the repository on PYTHONPATH
    so a child can import fsoctl."""
    env = dict(os.environ)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
    return env
