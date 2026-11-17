"""win_desktop: a hidden desktop per session (cross-platform Plan B, Task 5), with user32
replaced by a fake. tests/winlive/test_desktop_live.py runs the real calls on Windows."""
import ctypes
import sys

import pytest

from fsoctl.platform import win_api, win_desktop, win_scope


class FakeUser:
    """user32 as win_desktop uses it."""

    def __init__(self, fail=(), station="WinSta0", windows=(), busy_closes=0):
        self.fail, self.station, self.windows = set(fail), station, list(windows)
        self.calls, self.closed, self.posted = [], [], []
        self.busy_closes = busy_closes     # how many CloseDesktop calls fail first

    def CreateDesktopW(self, *arguments):
        name, _device, _devmode, _flags, access, _security = arguments
        self.calls.append(("CreateDesktopW", name, access))
        self.create_flags = _flags
        return None if "CreateDesktopW" in self.fail else 800

    def OpenDesktopW(self, name, flags, inherit, access):
        self.calls.append(("OpenDesktopW", name))
        self.open_arguments = (flags, inherit, access)
        return None if "OpenDesktopW" in self.fail else 810

    def CloseDesktop(self, handle):
        if self.busy_closes:
            self.busy_closes -= 1
            return 0
        self.closed.append(handle)
        return 1

    def SetThreadDesktop(self, handle):
        self.calls.append(("SetThreadDesktop", handle))
        return 0 if "SetThreadDesktop" in self.fail else 1

    def GetProcessWindowStation(self):
        return 820

    def GetUserObjectInformationW(self, handle, index, buffer, size, needed):
        assert handle == 820 and index == win_api.UOI_NAME
        buffer.value = self.station
        return 0 if "GetUserObjectInformationW" in self.fail else 1

    def EnumWindows(self, callback, param):
        if "EnumWindows" in self.fail:
            return 0
        for hwnd in (w[0] for w in self.windows):
            if not callback(hwnd, param):
                return 0            # as Windows does when the callback stops the listing
        return 1

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = next(w[1] for w in self.windows if w[0] == hwnd)
        return 1

    def IsWindowVisible(self, hwnd):
        return next(w[2] for w in self.windows if w[0] == hwnd)

    def PostMessageW(self, hwnd, message, wparam, lparam):
        self.posted.append((hwnd, message))
        return 1


@pytest.fixture
def user(monkeypatch):
    def make(**kw):
        fake = FakeUser(**kw)
        monkeypatch.setattr(win_api, "user32", lambda: fake)
        monkeypatch.setattr(win_api, "enum_callback", lambda fn: fn)
        monkeypatch.setattr(win_api, "last_error", lambda: 5)
        return fake
    return make


def test_create_makes_a_desktop_and_path_names_it_with_its_window_station(user):
    u = user()
    assert win_desktop.create("fso-harness-s0000abcd") == 800
    assert u.calls[0] == ("CreateDesktopW", "fso-harness-s0000abcd", win_api.GENERIC_ALL)
    assert win_desktop.path("fso-harness-s0000abcd") == "WinSta0\\fso-harness-s0000abcd"
    user(fail=["CreateDesktopW"])
    with pytest.raises(OSError) as e:
        win_desktop.create("x")
    assert "CreateDesktopW error 5" in str(e.value)
    user(fail=["GetUserObjectInformationW"])
    with pytest.raises(OSError):
        win_desktop.path("x")


def test_on_desktop_runs_the_function_on_another_thread_attached_to_the_desktop(user):
    import threading  # noqa: PLC0415
    u = user()
    main_thread = threading.get_ident()
    seen = {}

    def work():
        seen["thread"] = threading.get_ident()
        return 42
    assert win_desktop.on_desktop("fso-harness-s0000abcd", work) == 42
    assert seen["thread"] != main_thread
    assert ("OpenDesktopW", "fso-harness-s0000abcd") in u.calls
    assert ("SetThreadDesktop", 810) in u.calls and u.closed == [810]


def test_on_desktop_keeps_trying_to_release_the_desktop_and_says_so_if_it_cannot(
        user, monkeypatch, capsys):
    # Found on real Windows (2026-11-16): closing the handle fails while the worker thread is
    # still winding down; a handle left open keeps the session's desktop alive for good.
    monkeypatch.setattr(win_desktop.time, "sleep", lambda s: None)
    u = user(busy_closes=3)
    assert win_desktop.on_desktop("d", lambda: 1) == 1
    assert u.closed == [810] and capsys.readouterr().err == ""
    u = user(busy_closes=win_desktop.CLOSE_TRIES)
    assert win_desktop.on_desktop("d", lambda: 1) == 1          # the work itself succeeded
    assert u.closed == [] and "could not release desktop d" in capsys.readouterr().err


def test_on_desktop_with_no_name_runs_on_the_callers_own_desktop(user):
    u = user()
    assert win_desktop.on_desktop(None, lambda: "here") == "here"
    assert u.calls == []                               # a visible session: nothing to open


def test_on_desktop_passes_the_functions_error_on_and_reports_a_missing_desktop(user):
    u = user()

    def boom():
        raise ValueError("inside")
    with pytest.raises(ValueError, match="inside"):
        win_desktop.on_desktop("d", boom)
    assert u.closed == [810]
    user(fail=["OpenDesktopW"])
    with pytest.raises(OSError) as e:
        win_desktop.on_desktop("d", lambda: 1)
    assert "desktop d" in str(e.value)
    u = user(fail=["SetThreadDesktop"])
    with pytest.raises(OSError):
        win_desktop.on_desktop("d", lambda: 1)
    assert u.closed == [810]


def test_windows_of_lists_the_visible_top_level_windows_of_one_process(user):
    user(windows=[(1, 100, 1), (2, 200, 1), (3, 100, 0), (4, 100, 1)])
    assert win_desktop.windows_of(100) == [1, 4]       # 3 is hidden, 2 is another process
    assert win_desktop.windows_of(300) == []


def test_ask_close_posts_a_close_message_to_each_window(user):
    u = user(windows=[(1, 100, 1), (4, 100, 1)])
    assert win_desktop.ask_close(100) == 2
    assert u.posted == [(1, win_api.WM_CLOSE), (4, win_api.WM_CLOSE)]


# ---- gaps a mutation run found (QA Plan B, MU-18): each test names what it pins ----
def test_work_that_does_not_finish_is_given_up_on_and_cannot_hold_the_command_open(
        user, monkeypatch):
    import threading  # noqa: PLC0415
    made = []

    class Recorded(threading.Thread):
        def __init__(self, *arguments, **keywords):
            made.append(keywords)
            super().__init__(*arguments, **keywords)
    u = user()
    monkeypatch.setattr(win_desktop.threading, "Thread", Recorded)
    monkeypatch.setattr(win_desktop, "ON_DESKTOP_WAIT_S", 0.05)
    gate = threading.Event()
    try:
        with pytest.raises(OSError, match="work on desktop d did not finish in 0 s"):
            # 5 s: far past the wait under test, yet no test run waits on it for ever
            win_desktop.on_desktop("d", lambda: gate.wait(5))
    finally:
        gate.set()
    # a daemon thread: one stuck in a Windows call must not keep `fsoctl` from exiting
    assert made[0]["daemon"] is True
    # the handle is NOT closed while the worker may still be attached through it
    assert u.closed == []


def test_the_desktop_is_opened_for_this_process_alone(user):
    u = user()
    win_desktop.on_desktop("d", lambda: 1)
    # no flags (1 would let other accounts hook it), a handle no child process inherits
    assert u.open_arguments == (0, False, win_api.GENERIC_ALL)
    win_desktop.create("d")
    assert u.create_flags == 0


def test_a_busy_desktop_is_retried_after_the_set_gap(user, monkeypatch):
    gaps = []
    monkeypatch.setattr(win_desktop.time, "sleep", gaps.append)
    u = user(busy_closes=2)
    win_desktop.on_desktop("d", lambda: 1)
    assert gaps == [win_desktop.CLOSE_GAP_S] * 2 and u.closed == [810]


def test_the_window_listing_stops_at_its_limit(user, monkeypatch):
    monkeypatch.setattr(win_desktop, "MAX_WINDOWS", 3)
    user(windows=[(n, 100, 1) for n in range(1, 6)])
    assert win_desktop.windows_of(100) == [1, 2, 3]


class FakeKernelForDesktop:
    """Only what win_scope.start needs once a desktop is named; see test_platform_scope.py
    for the full fake. Records the handle list and what was closed."""

    def __init__(self):
        self.closed, self.handle_list, self.desktop, self.made_inheritable = [], None, None, []

    def CreateJobObjectW(self, attributes, name):
        return 500

    def SetHandleInformation(self, handle, mask, flags):
        self.made_inheritable.append(handle)
        return 1

    def InitializeProcThreadAttributeList(self, buffer, count, flags, size):
        if buffer is None:
            size._obj.value = 48
            return 0
        return 1

    def UpdateProcThreadAttribute(self, *arguments):
        self.handle_list = list(arguments[3])
        return 1

    def DeleteProcThreadAttributeList(self, buffer):
        return None

    def CreateProcessW(self, *arguments):
        si, pi = arguments[8], arguments[9]
        self.desktop = si._obj.lpDesktop
        pi._obj.hProcess, pi._obj.hThread, pi._obj.dwProcessId = 700, 701, 4242
        return 1

    def AssignProcessToJobObject(self, job, process):
        return 1

    def ResumeThread(self, thread):
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def test_start_makes_the_named_desktop_and_the_child_inherits_a_handle_to_it(
        user, monkeypatch, tmp_path):
    # the same reason as the job handle: a desktop goes away when nothing holds it, and the
    # launching command exits; the runner and the engine carry a handle for as long as they live
    u = user()
    k = FakeKernelForDesktop()
    monkeypatch.setattr(win_api, "kernel32", lambda: k)
    monkeypatch.setattr(win_api, "last_error", lambda: 0)
    monkeypatch.setattr(win_scope, "_inheritable_handle", lambda f: 900)
    monkeypatch.setattr(win_scope.shutil, "which", lambda name, path=None: name)
    win_scope.start("fso-harness-s0000abcd", ["python"], {"PATH": "p"}, tmp_path / "r.log",
                    ["Desktop=fso-harness-s0000abcd"])
    assert u.calls[0][:2] == ("CreateDesktopW", "fso-harness-s0000abcd")
    assert k.desktop == "WinSta0\\fso-harness-s0000abcd"
    assert k.handle_list == [900, 500, 800] and 800 in k.made_inheritable
    assert u.closed == [800]                           # this command lets go of its handle


def test_the_structures_have_their_windows_sizes():
    # 64-bit layouts from the Windows headers; a wrong size makes the real calls fail
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(win_api.STARTUPINFOW) == 104
        assert ctypes.sizeof(win_api.STARTUPINFOEXW) == 112
        assert ctypes.sizeof(win_api.PROCESS_INFORMATION) == 24
        assert ctypes.sizeof(win_api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
        assert ctypes.sizeof(win_api.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) == 48
        assert ctypes.sizeof(win_api.MEMORYSTATUSEX) == 64
    # These two hold C "long" fields, which are 4 bytes on Windows and 8 on 64-bit Linux:
    # their sizes can only be right, and so only be checked, on Windows itself.
    if sys.platform == "win32":
        assert ctypes.sizeof(win_api.RECT) == 16
        assert ctypes.sizeof(win_api.BITMAPINFOHEADER) == 40


def test_a_window_listing_that_fails_is_an_error_and_not_no_windows(user):
    # QA Plan B, CR-23: "no windows" is reported to the user as "the engine may still be
    # starting"; a listing Windows refused is something else
    user(fail=["EnumWindows"], windows=[(1, 100, 1)])
    with pytest.raises(OSError, match="EnumWindows"):
        win_desktop.windows_of(100)


def test_a_zero_from_the_window_listing_with_no_error_code_is_an_empty_listing(user,
                                                                             monkeypatch):
    # Measured on real Windows (2026-11-17, tests/winlive/test_desktop_live.py): while a
    # session desktop was polled for a window still being created, EnumWindows answered
    # zero with error code 0. That is "nothing listed", not a failure.
    user(fail=["EnumWindows"], windows=[(1, 100, 1)])
    monkeypatch.setattr(win_api, "last_error", lambda: 0)
    assert win_desktop.windows_of(100) == []


def test_work_on_the_users_own_desktop_has_the_same_time_limit(user, monkeypatch):
    # cross-family review XFA-7: a visible session's work ran in the calling thread with no
    # limit, so a window call that never returned held `fsoctl screenshot` open for good
    import threading  # noqa: PLC0415
    u = user()
    monkeypatch.setattr(win_desktop, "ON_DESKTOP_WAIT_S", 0.05)
    gate = threading.Event()
    try:
        with pytest.raises(OSError, match="the user's desktop did not finish in 0 s"):
            win_desktop.on_desktop(None, lambda: gate.wait(5))
    finally:
        gate.set()
    assert u.calls == []                               # still nothing to open or attach to


def test_an_error_in_work_on_the_users_own_desktop_is_raised_to_the_caller(user):
    user()

    def boom():
        raise ValueError("inside")
    with pytest.raises(ValueError, match="inside"):
        win_desktop.on_desktop(None, boom)
