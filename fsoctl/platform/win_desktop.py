"""A desktop of its own for each Windows session, so the session is out of sight.

A Windows "desktop" is a separate surface of windows inside the user's logon: programs
started on one are not shown on another, never appear in the taskbar or Alt-Tab of the user's
desktop, and cannot take its keyboard, mouse or focus. A session's runner and engine are
started on a desktop named after the session (win_scope.start does that when it is given a
"Desktop=" property); this is the Windows counterpart of the Xvfb display a Linux session
gets. Measured with the engine and retail data on 2026-11-16: it starts, draws and can be
captured there (docs/research/2026-11-16-xplat-vm-results.md, probe 9).

A desktop exists while something holds it: a process running on it, or an open handle. The
launching command exits, so the session's first process inherits a handle to its desktop
(win_scope.start), the same way and for the same reason as its job handle.

A later fsoctl call (screenshot, the runner's close request) reaches a session's windows by
attaching one of its own threads to that desktop for the length of one function: on_desktop.
"""
import ctypes
import sys
import threading
import time

from . import win_api

ON_DESKTOP_WAIT_S = 60.0     # the longest one function may run on a session's desktop
MAX_WINDOWS = 4096           # top-level windows looked at in one listing
NAME_CHARS = 256             # a window station's name is far shorter
CLOSE_TRIES = 100            # tries to release a desktop handle after its worker thread ended
CLOSE_GAP_S = 0.02           # pause between them (two seconds in all)


def create(name: str):
    """Create the desktop `name` in this logon's window station and return a handle to it
    (opens it if it already exists). Raises OSError."""
    handle = win_api.user32().CreateDesktopW(name, None, None, 0, win_api.GENERIC_ALL, None)
    if not handle:
        raise OSError(f"CreateDesktopW error {win_api.last_error()} for desktop {name}")
    return handle


def close(handle) -> None:
    """Close a desktop handle from create(). The result is not checked: the callers are
    on their way out of a failed start or have just started the session, and a handle that
    could not be closed goes with this process."""
    _ = win_api.user32().CloseDesktop(handle)


def path(name: str) -> str:
    """Return "<window station>\\<name>", the form a new process is given as its desktop
    (STARTUPINFO.lpDesktop). The station is this process's own ("WinSta0" for a logged-in
    user). Raises OSError when Windows will not name it."""
    u = win_api.user32()
    buffer = ctypes.create_unicode_buffer(NAME_CHARS)
    needed = win_api.DWORD(0)
    if not u.GetUserObjectInformationW(u.GetProcessWindowStation(), win_api.UOI_NAME, buffer,
                                       ctypes.sizeof(buffer), ctypes.byref(needed)):
        raise OSError(f"cannot name this window station (error {win_api.last_error()})")
    return f"{buffer.value}\\{name}"


def on_desktop(name: str | None, fn):
    """Run fn() with the calling code attached to the desktop `name`, and return its value;
    an exception from fn is raised here. With name None (a visible session) nothing is
    opened or attached: fn runs on the desktop this process is on, in a worker all the same,
    so the time limit below holds there too (cross-family review XFA-7: run in the calling
    thread, a window call that never returned would hold the command open for good).

    WHY a fresh thread: which desktop window calls see is a property of the THREAD, and a
    thread that already owns windows or hooks cannot be moved. A new thread has none. The
    desktop handle is opened and closed by the calling thread, around the worker's life:
    Windows refuses to close a desktop handle while a thread is attached through it."""
    u = handle = None
    if name is not None:
        u = win_api.user32()
        handle = u.OpenDesktopW(name, 0, False, win_api.GENERIC_ALL)
        if not handle:
            raise OSError(f"cannot open desktop {name} (error {win_api.last_error()}); "
                          "has the session ended?")
    box: dict = {}

    def body() -> None:
        """The worker: attach to the desktop (if one was named), run fn, keep its result or
        its error."""
        if handle is not None and not u.SetThreadDesktop(handle):
            box["error"] = OSError(f"cannot attach to desktop {name} "
                                   f"(error {win_api.last_error()})")
            return
        try:
            box["value"] = fn()
        except Exception as e:     # handed to the caller's thread, raised there
            box["error"] = e

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    worker.join(ON_DESKTOP_WAIT_S)
    if worker.is_alive():
        where = f"desktop {name}" if name is not None else "the user's desktop"
        raise OSError(f"work on {where} did not finish in {ON_DESKTOP_WAIT_S:.0f} s")
    if handle is not None:
        _release(u, handle, name)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _release(u, handle, name: str) -> None:
    """Close a desktop handle whose worker thread has just ended, trying again for a short
    while: Windows refuses (busy) until that thread is completely gone, a moment after
    Python reports it finished. WHY it matters (seen on real Windows, 2026-11-16): a handle
    left open keeps the desktop in existence after its session ended. If it still cannot be
    closed, that is said on stderr; the handle then lasts until this command exits."""
    for _ in range(CLOSE_TRIES):                  # bounded
        if u.CloseDesktop(handle):
            return
        time.sleep(CLOSE_GAP_S)
    sys.stderr.write(f"fsoctl: could not release desktop {name} (error "
                     f"{win_api.last_error()}); it stays until this command exits\n")


def windows_of(pid: int) -> list[int]:
    """Handles of the visible top-level windows of process pid on the calling thread's
    desktop (call it through on_desktop for a hidden session). Windows itself lists them;
    the program is not asked anything."""
    u = win_api.user32()
    found: list[int] = []
    seen = [0]

    def visit(hwnd, _param) -> bool:
        """EnumWindows callback: keep the visible windows of this process."""
        seen[0] += 1
        owner = win_api.DWORD(0)
        # Not checked: a window that went away leaves the owner 0, which matches no pid.
        _ = u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and u.IsWindowVisible(hwnd):
            found.append(hwnd)
        return seen[0] < MAX_WINDOWS              # False stops the listing: bounded

    callback = win_api.enum_callback(visit)       # kept in a name until EnumWindows returns
    # A failed listing must not read as "no windows": the caller reports that as an engine
    # still starting. But zero is not only what a failed EnumWindows returns: it also
    # returns zero when the callback stopped the listing (Microsoft's page for it), and,
    # measured on real Windows (2026-11-17, tests/winlive/test_desktop_live.py), with error
    # code 0 while a session desktop was polled for a window still being created. So only a
    # zero WITH an error code, from a listing this code did not stop, is a failure; the
    # error slot is emptied first so that an older code is not read.
    win_api.clear_last_error()
    listed = u.EnumWindows(callback, None)
    if not listed and seen[0] < MAX_WINDOWS and win_api.last_error():
        raise OSError(f"EnumWindows error {win_api.last_error()}")
    return found


def ask_close(pid: int) -> int:
    """Post a close request (WM_CLOSE, what the window's X button sends) to every visible
    top-level window of process pid on the calling thread's desktop; return how many. The
    engine turns it into its own orderly quit, as it does SIGTERM on Linux. Posting does not
    wait for the window to act."""
    u = win_api.user32()
    windows = windows_of(pid)
    for hwnd in windows:
        # Not checked: a window that went away between the listing and the post is the
        # outcome wanted.
        _ = u.PostMessageW(hwnd, win_api.WM_CLOSE, 0, 0)
    return len(windows)
