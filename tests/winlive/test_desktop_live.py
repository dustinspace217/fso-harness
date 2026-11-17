"""win_desktop against real Windows desktops (plan B Task 5, Step 2): a process started on a
session's desktop has its window there and nowhere else, can be asked to close from another
process, and the desktop is gone once nothing holds it."""
import sys
import time

import pytest

from fsoctl.platform import win_desktop, win_procs, win_scope

# A window that stays up for a minute unless it is asked to close.
WINDOW = ("import tkinter as t\n"
          "r = t.Tk()\n"
          "r.title('fso-harness test window')\n"
          "r.after(60000, r.destroy)\n"
          "r.mainloop()\n")


def wait_for(condition, seconds=20.0):
    """Poll `condition` until true or the time is up; return its last value."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:            # bounded
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    return condition()


def test_a_window_on_a_session_desktop_is_found_there_and_not_on_the_users(
        tmp_path, job, child_env):
    desktop = job                                   # named like the job, as a session's is
    started = win_scope.start(job, [sys.executable, "-c", WINDOW], child_env,
                              tmp_path / "w.log", [f"Desktop={desktop}"])

    def its_windows():
        return win_desktop.on_desktop(desktop, lambda: win_desktop.windows_of(started.pid))
    assert wait_for(its_windows), (tmp_path / "w.log").read_text(errors="replace")
    assert win_desktop.windows_of(started.pid) == []        # this thread: the user's desktop
    # the command that started it holds no handle any more; the desktop is still there
    assert win_desktop.path(desktop).endswith("\\" + desktop)
    asked = win_desktop.on_desktop(desktop, lambda: win_desktop.ask_close(started.pid))
    assert asked >= 1
    assert wait_for(lambda: win_procs.gone(started.pid)), "the window ignored the close request"
    # nothing runs on the desktop and nothing holds it: it no longer exists
    assert wait_for(lambda: _cannot_open(desktop))


def _cannot_open(desktop: str) -> bool:
    """True when the desktop can no longer be opened by name."""
    try:
        win_desktop.on_desktop(desktop, lambda: None)
    except OSError:
        return True
    return False


def test_on_desktop_reports_a_desktop_that_does_not_exist():
    with pytest.raises(OSError) as e:
        win_desktop.on_desktop("fso-harness-no-such-desktop", lambda: 1)
    assert "has the session ended?" in str(e.value)
