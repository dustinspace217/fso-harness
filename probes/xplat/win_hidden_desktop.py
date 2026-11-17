"""Probe 1 (cross-platform Plan A, Task 5): can a session live on a hidden Windows desktop?

The design keeps harness sessions out of the user's sight on Windows by giving them a desktop of
their own (CreateDesktopW): windows there are never shown, never take focus, and never receive
the user's keyboard. This probe measures what still works on such a desktop:

    gl_context / pixels_match   gl_square.py asks for an OpenGL 3.2 core context; gl_context is
                                the version it got, gl_meets_minimum whether that is at least 3.2
                                (SDL can fall back to an older one), pixels_match whether its
                                glReadPixels returns the clear colour
    capture_matches             PrintWindow from a thread switched to that desktop returns the
                                same colour (screenshots work)
    uia_sees_window             UI Automation, from a thread on that desktop, finds the window
                                under the root element (editor driving can reach it)
    postmessage_key_reached     a posted WM_KEYDOWN Space arrives as an SDL key event (input works)
    fso_reached_gl_init         the FSO 26.0.1 FastDebug engine, started there with -window,
                                sound off and no game data, logs "OpenGL init is complete!";
                                FastDebug because release builds write no log (plan deviation)

Each check runs four times: {runner's OpenGL, Mesa's software OpenGL} x {normal desktop as a
control, hidden desktop}. A false on the hidden desktop next to a true on the control is a
platform limit; false on both is more likely the probe. Every failure keeps its error text in
"notes". The probe never fails the job (exit 0 whatever it finds): it is a measurement.

    python win_hidden_desktop.py --out DIR --fso-dir DIR [--mesa-dir DIR]
"""
import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

EXPECT = (64, 128, 191)        # gl_square.CLEAR (0.25, 0.5, 0.75) in 8-bit channels
TOLERANCE = 2                  # per channel; 0.25 * 255 = 63.75 lands on 63 or 64 by driver
POLL_S = 0.25
READY_WAIT_S = 20              # plan: wait up to 20 s for the stand-in's ready file
KEY_WAIT_S = 5
FSO_WAIT_S = 30
THREAD_WAIT_S = 30
EXIT_WAIT_MS = 5000
LOG_TAIL_LINES = 15            # kept in notes when the engine did not reach GL init
DESKTOP_NAME = "fsoharness-probe1"
GL_INIT_LINE = "OpenGL init is complete"
MESA_DLLS = ("opengl32.dll", "libgallium_wgl.dll")    # mesa-dist-win 26.2.4, x64 folder
FSO_EXE = "fs2_open_26_0_1_x64_SSE2-FASTDBG.exe"
GL_SQUARE = Path(__file__).resolve().with_name("gl_square.py")

GENERIC_ALL = 0x10000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
UOI_NAME = 2
PW_CLIENTONLY, PW_RENDERFULLCONTENT = 0x1, 0x2
CLR_INVALID = 0xFFFFFFFF
WM_KEYDOWN, WM_KEYUP, VK_SPACE = 0x0100, 0x0101, 0x20
SPACE_DOWN_LPARAM, SPACE_UP_LPARAM = 0x00390001, 0xC0390001   # repeat 1, scan code 0x39
TREESCOPE_CHILDREN, UIA_PROCESS_ID_PROPERTY = 2, 30002          # UIAutomationClient.h values


def colour_close(rgb, expect=EXPECT, tolerance=TOLERANCE) -> bool:
    """True when rgb has three channels, each within tolerance of expect."""
    return len(rgb) == 3 and all(abs(int(a) - int(b)) <= tolerance
                                 for a, b in zip(rgb, expect, strict=True))


def wait_for(predicate, seconds: float) -> bool:
    """Poll predicate every POLL_S until it is true or seconds pass; return its last answer."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:        # bounded by the deadline
        if predicate():
            return True
        time.sleep(POLL_S)
    return bool(predicate())


def env_block(env: dict) -> str:
    """Return env as a CreateProcessW Unicode environment block: NAME=value NUL ... NUL.
    Windows wants the names sorted case-insensitively."""
    pairs = sorted(env.items(), key=lambda kv: kv[0].upper())
    return "".join(f"{k}={v}\0" for k, v in pairs) + "\0"


def find_log(prefs: Path, game_dir: Path):
    """Return the fs2_open.log the engine wrote, or None. The probe points the engine's
    preferences (and so its log) at the cell's own prefs folder with FSO_PREFERENCES_PATH, so the
    real profile is never read, written or deleted (QA CR-6); in legacy or portable mode on
    Windows the log is ./data under the current folder, the game folder (os_get_config_path in
    osapi.cpp, outwnd.cpp, at 26.0.1)."""
    for path in (prefs / "data" / "fs2_open.log", game_dir / "data" / "fs2_open.log"):
        if path.exists():
            return path
    return None


def log_lines(path, *needles) -> list:
    """Return the stripped lines of path that contain any needle ([] when path is None)."""
    if path is None:
        return []
    return [ln.strip() for ln in path.read_text(errors="replace").splitlines()
            if any(n in ln for n in needles)]


def win32() -> SimpleNamespace:
    """Load user32, gdi32 and kernel32 with the signatures this probe calls (handles must be
    pointer-sized on 64-bit Windows, or ctypes truncates them), plus the two structures
    CreateProcessW needs. Windows only; nothing here runs at import time."""
    from ctypes import wintypes as wt  # noqa: PLC0415 - Windows-only; the unit tier imports this file

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                    ("lpTitle", wt.LPWSTR)] + [(n, wt.DWORD) for n in (
                        "dwX", "dwY", "dwXSize", "dwYSize", "dwXCountChars", "dwYCountChars",
                        "dwFillAttribute", "dwFlags")] + [
                    ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE),
                    ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                    ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]

    u = ctypes.WinDLL("user32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H, P = wt.HANDLE, ctypes.POINTER
    for fn, res, args in (
            (u.CreateDesktopW, H, [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD,
                                   ctypes.c_void_p]),
            (u.OpenDesktopW, H, [wt.LPCWSTR, wt.DWORD, wt.BOOL, wt.DWORD]),
            (u.CloseDesktop, wt.BOOL, [H]),
            (u.SetThreadDesktop, wt.BOOL, [H]),
            (u.GetProcessWindowStation, H, []),
            (u.GetUserObjectInformationW, wt.BOOL, [H, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                                    P(wt.DWORD)]),
            (u.GetClientRect, wt.BOOL, [wt.HWND, P(wt.RECT)]),
            (u.GetDC, wt.HDC, [wt.HWND]),
            (u.ReleaseDC, ctypes.c_int, [wt.HWND, wt.HDC]),
            (u.PrintWindow, wt.BOOL, [wt.HWND, wt.HDC, wt.UINT]),
            (u.PostMessageW, wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
            (g.CreateCompatibleDC, wt.HDC, [wt.HDC]),
            (g.CreateCompatibleBitmap, wt.HBITMAP, [wt.HDC, ctypes.c_int, ctypes.c_int]),
            (g.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]),
            (g.GetPixel, wt.DWORD, [wt.HDC, ctypes.c_int, ctypes.c_int]),
            (g.DeleteObject, wt.BOOL, [wt.HGDIOBJ]),
            (g.DeleteDC, wt.BOOL, [wt.HDC]),
            (k.CreateProcessW, wt.BOOL, [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                         wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
                                         P(STARTUPINFOW), P(PROCESS_INFORMATION)]),
            (k.TerminateProcess, wt.BOOL, [H, wt.UINT]),
            (k.WaitForSingleObject, wt.DWORD, [H, wt.DWORD]),
            (k.CloseHandle, wt.BOOL, [H])):
        fn.restype, fn.argtypes = res, args
    return SimpleNamespace(u=u, g=g, k=k, wt=wt, STARTUPINFOW=STARTUPINFOW,
                           PROCESS_INFORMATION=PROCESS_INFORMATION)


def station_name(w) -> str:
    """Return this process's window station name (WinSta0 on an interactive logon)."""
    buf = ctypes.create_unicode_buffer(256)
    need = w.wt.DWORD()
    if not w.u.GetUserObjectInformationW(w.u.GetProcessWindowStation(), UOI_NAME, buf,
                                         ctypes.sizeof(buf), ctypes.byref(need)):
        return f"? (GetUserObjectInformationW error {ctypes.get_last_error()})"
    return buf.value


def spawn(w, argv: list, desktop: str, env: dict, cwd: Path):
    """Start argv on desktop ("station\\desktop") with env; return (process handle, pid).
    Raises OSError with Windows' error text when CreateProcessW fails."""
    si = w.STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop
    pi = w.PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))   # may be written to
    envbuf = ctypes.create_unicode_buffer(env_block(env))   # keeps the embedded NULs
    if not w.k.CreateProcessW(None, cmdline, None, None, False, CREATE_UNICODE_ENVIRONMENT,
                              envbuf, str(cwd), ctypes.byref(si), ctypes.byref(pi)):
        raise ctypes.WinError(ctypes.get_last_error())
    w.k.CloseHandle(pi.hThread)
    return pi.hProcess, pi.dwProcessId


def end_process(w, handle, grace_ms: int = EXIT_WAIT_MS) -> str | None:
    """Give the process grace_ms to exit by itself, then terminate it; close the handle.
    Returns None when it is gone, or text saying it could not be ended (for the notes)."""
    problem = None
    if w.k.WaitForSingleObject(handle, grace_ms) != 0:    # 0 = WAIT_OBJECT_0, it exited
        if not w.k.TerminateProcess(handle, 1):
            problem = f"TerminateProcess error {ctypes.get_last_error()}"
        if w.k.WaitForSingleObject(handle, EXIT_WAIT_MS) != 0:
            problem = (problem or "") + f" still running {EXIT_WAIT_MS} ms after termination"
    w.k.CloseHandle(handle)
    return problem


def on_desktop(w, hdesk, fn):
    """Run fn() in a new thread switched to hdesk (or left on the normal desktop when hdesk is
    None); return (value, error text or None). A thread may switch desktop only while it owns no
    windows, which a fresh thread never does."""
    box = {}

    def body():
        if hdesk and not w.u.SetThreadDesktop(hdesk):
            box["err"] = f"SetThreadDesktop error {ctypes.get_last_error()}"
            return
        try:
            box["val"] = fn()
        except Exception as e:     # a measurement: any failure is recorded, never raised
            box["err"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=body, daemon=True)
    t.start()
    t.join(THREAD_WAIT_S)
    if t.is_alive():
        return None, f"no answer in {THREAD_WAIT_S} s"
    return box.get("val"), box.get("err")


def capture_centre(w, hwnd: int) -> tuple:
    """PrintWindow the client area of hwnd into a memory bitmap; return its centre (r, g, b).
    PW_RENDERFULLCONTENT asks for GPU-drawn content as well; whether it arrives is what
    capture_matches measures."""
    rect = w.wt.RECT()
    if not w.u.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    hdc = w.u.GetDC(hwnd)
    if not hdc:
        raise ctypes.WinError(ctypes.get_last_error())
    mem = w.g.CreateCompatibleDC(hdc)
    bmp = w.g.CreateCompatibleBitmap(hdc, rect.right, rect.bottom)
    old = w.g.SelectObject(mem, bmp)
    try:
        if not w.u.PrintWindow(hwnd, mem, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            raise ctypes.WinError(ctypes.get_last_error())
        px = w.g.GetPixel(mem, rect.right // 2, rect.bottom // 2)
    finally:
        w.g.SelectObject(mem, old)
        w.g.DeleteObject(bmp)
        w.g.DeleteDC(mem)
        w.u.ReleaseDC(hwnd, hdc)
    if px == CLR_INVALID:
        raise RuntimeError("GetPixel returned CLR_INVALID")
    return colorref_rgb(px)


def colorref_rgb(px: int) -> tuple:
    """Unpack a Win32 COLORREF (0x00BBGGRR) into (r, g, b)."""
    return (px & 0xFF, (px >> 8) & 0xFF, (px >> 16) & 0xFF)


def uia_finds(pid: int) -> bool:
    """True when UI Automation finds a top-level element of process pid under the root."""
    # Windows-only packages, imported where used so the unit tier can import this file.
    import comtypes  # noqa: PLC0415
    import comtypes.client  # noqa: PLC0415
    comtypes.CoInitialize()
    try:
        # Generated by comtypes.client.GetModule("UIAutomationCore.dll") in main().
        from comtypes.gen.UIAutomationClient import (  # noqa: PLC0415
            CUIAutomation,
            IUIAutomation,
        )
        uia = comtypes.client.CreateObject(CUIAutomation, interface=IUIAutomation)
        cond = uia.CreatePropertyCondition(UIA_PROCESS_ID_PROPERTY, pid)
        return bool(uia.GetRootElement().FindFirst(TREESCOPE_CHILDREN, cond))
    finally:
        comtypes.CoUninitialize()


def post_space(w, hwnd: int) -> bool:
    """Post a Space key-down and key-up to hwnd; True when both posts were accepted."""
    if not (w.u.PostMessageW(hwnd, WM_KEYDOWN, VK_SPACE, SPACE_DOWN_LPARAM)
            and w.u.PostMessageW(hwnd, WM_KEYUP, VK_SPACE, SPACE_UP_LPARAM)):
        raise ctypes.WinError(ctypes.get_last_error())
    return True


def window_checks(cell, ready: dict) -> None:
    """Capture, UI Automation and input checks against the stand-in's window; fills cell.r and
    cell.notes."""
    w, hdesk, r, notes = cell.w, cell.hdesk, cell.r, cell.notes
    hwnd = ready.get("hwnd")
    if not hwnd:
        notes.append("stand-in reported no window handle")
        return
    rgb, err = on_desktop(w, hdesk, lambda: capture_centre(w, hwnd))
    r["capture_matches"] = rgb is not None and colour_close(rgb)
    notes.append(f"PrintWindow centre {rgb}" if err is None else f"PrintWindow: {err}")
    found, err = on_desktop(w, hdesk, lambda: uia_finds(ready["pid"]))
    r["uia_sees_window"] = bool(found)
    if err:
        notes.append(f"UI Automation: {err}")
    _, err = on_desktop(w, hdesk, lambda: post_space(w, hwnd))
    if err:
        notes.append(f"PostMessage: {err}")
    r["postmessage_key_reached"] = wait_for((cell.work / "key.txt").exists, KEY_WAIT_S)


def stand_in_checks(cell) -> None:
    """Start gl_square.py on the cell's desktop, then run every check on its window."""
    w, r, notes = cell.w, cell.r, cell.notes
    argv = [sys.executable, str(GL_SQUARE), str(cell.work)]
    try:
        handle, _pid = spawn(w, argv, cell.desktop, cell.env, cell.work)
    except OSError as e:
        notes.append(f"CreateProcessW (stand-in): {e}")
        return
    try:
        ready_path = cell.work / "ready.json"
        if not wait_for(ready_path.exists, READY_WAIT_S):
            notes.append(f"stand-in wrote no ready.json in {READY_WAIT_S} s")
            return
        ready = json.loads(ready_path.read_text())
        if "error" in ready:
            r["gl_context"] = "error: " + ready["error"]
            return
        r["gl_context"], r["gl_renderer"] = ready["gl_version"], ready["gl_renderer"]
        r["pixels_match"] = colour_close(ready["pixel"])
        notes.append(f"glReadPixels centre {ready['pixel']}")
        window_checks(cell, ready)
    finally:
        (cell.work / "stop").write_text("")
        problem = end_process(w, handle)
        if problem:
            notes.append(f"stand-in: {problem}")


def fso_check(cell) -> bool:
    """Start the engine on the cell's desktop with -window from an empty folder; True when its
    log shows OpenGL init completed within FSO_WAIT_S. With no game data the engine then stops at
    a modal error box, so it is always terminated afterwards."""
    game, prefs = cell.work / "game", cell.work / "prefs"     # fresh per cell: no stale log
    game.mkdir()
    prefs.mkdir()
    env = dict(cell.env, FSO_PREFERENCES_PATH=str(prefs))
    # WHY -nosound: the runner has no audio device, and in run 40251954153 the engine's log
    # stopped at "Initializing OpenAL..." in every cell, before graphics. snd_init returns at
    # once with sound off (sound.cpp); audio is probe 6's question, not this one's.
    argv = [str(cell.fso_exe), "-window", "-nosound", "-nomusic"]
    try:
        handle, _pid = spawn(cell.w, argv, cell.desktop, env, game)
    except OSError as e:
        cell.notes.append(f"CreateProcessW (engine): {e}")
        return False
    try:
        seen = wait_for(lambda: bool(log_lines(find_log(prefs, game), GL_INIT_LINE)),
                        FSO_WAIT_S)
    finally:
        problem = end_process(cell.w, handle, grace_ms=0)
        if problem:
            cell.notes.append(f"engine: {problem}")
    log = find_log(prefs, game)
    cell.notes.append(f"engine log: {log}" if log else "engine log: none written")
    cell.notes.extend(f"engine: {ln}"
                      for ln in log_lines(log, "OpenGL Renderer", "OpenGL Version"))
    if log is not None and not seen:
        tail = log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]
        cell.notes.extend(f"engine log tail: {ln.strip()}" for ln in tail if ln.strip())
    return seen


def one_cell(cell) -> dict:
    """Run every check for one (OpenGL, desktop) cell in a fresh work folder; return its row.
    cell carries w, gl, desk, hdesk, desktop, env and fso_exe; r, notes and work are added here."""
    cell.r = {"opengl": cell.gl, "desktop": cell.desk,
              "desktop_created": cell.desk == "normal" or bool(cell.hdesk),
              "gl_context": None, "gl_renderer": None, "gl_meets_minimum": False,
              "pixels_match": False, "capture_matches": False, "uia_sees_window": False,
              "postmessage_key_reached": False, "fso_reached_gl_init": False}
    cell.notes = []
    if not cell.r["desktop_created"]:
        return {**cell.r, "notes": ["the hidden desktop was not created; nothing ran"]}
    # ignore_cleanup_errors: a child that has not let go of the folder must not cost the row.
    with tempfile.TemporaryDirectory(prefix="probe1-", ignore_cleanup_errors=True) as tmp:
        cell.work = Path(tmp)
        stand_in_checks(cell)
        cell.r["gl_meets_minimum"] = gl_meets_minimum(cell.r["gl_context"])
        cell.r["fso_reached_gl_init"] = fso_check(cell)
    return {**cell.r, "notes": cell.notes}


def gl_meets_minimum(version) -> bool:
    """True when a GL_VERSION string ("4.6 (Core Profile) Mesa ...", "1.1.0") is at least 3.2,
    the engine's minimum (gropengl.cpp at 26.0.1). The first CI run's runner cells reported
    working pixels and capture from a 1.1 context the engine then refused (QA CR-4)."""
    parts = str(version or "").split(" ")[0].split(".")
    if len(parts) < 2 or not (parts[0].isdecimal() and parts[1].isdecimal()):
        return False
    return (int(parts[0]), int(parts[1])) >= (3, 2)


def mesa_engine_dir(fso_dir: Path, mesa_dir: Path, out: Path) -> Path:
    """A copy of the engine (exe and DLLs) with Mesa's DLLs beside it, in a folder of this run's
    own, so the FSO folder given to the probe is never changed (QA TA-5: on a real install the
    copied DLLs would have stayed, and could have moved later launches onto Mesa)."""
    target = out / "fso-mesa"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for f in [fso_dir / FSO_EXE, *fso_dir.glob("*.dll")]:
        shutil.copy2(f, target / f.name)
    for name in MESA_DLLS:
        shutil.copy2(mesa_dir / name, target / name)
    return target


def mesa_env(mesa_dir: Path) -> dict:
    """Environment for the Mesa cells: SDL loads Mesa's opengl32.dll by full path for the
    stand-in, and the Mesa folder leads PATH so that DLL finds its own dependencies; the engine
    runs from mesa_engine_dir (the usual mesa-dist-win per-program install).
    GALLIUM_DRIVER pins Mesa's CPU renderer: on Windows Mesa may otherwise pick its D3D12 driver,
    which is not the software path the plan asks about."""
    env = dict(os.environ)
    env["GALLIUM_DRIVER"] = "llvmpipe"
    env["SDL_OPENGL_LIBRARY"] = str(mesa_dir / "opengl32.dll")
    env["PATH"] = f"{mesa_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def run_cells(w, args, station: str, hdesk) -> list:
    """Run the runner-OpenGL cells, then (with --mesa-dir) the Mesa cells; return the rows. A
    hidden row is written even when the desktop was not created, with desktop_created false."""
    rows = []
    for gl in ("runner", "mesa") if args.mesa_dir else ("runner",):
        if gl == "runner":
            env, engine_dir = dict(os.environ), args.fso_dir
        else:
            env = mesa_env(args.mesa_dir)
            engine_dir = mesa_engine_dir(args.fso_dir, args.mesa_dir, args.out)
        for desk, handle in (("normal", None), ("hidden", hdesk)):
            target = f"{station}\\{'Default' if desk == 'normal' else DESKTOP_NAME}"
            cell = SimpleNamespace(w=w, gl=gl, desk=desk, hdesk=handle, desktop=target, env=env,
                                   fso_exe=engine_dir / FSO_EXE)
            rows.append(one_cell(cell))
    return rows


def main() -> int:
    """Run the cells and write probe-hidden-desktop.json; always exit 0."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fso-dir", type=Path, required=True)
    ap.add_argument("--mesa-dir", type=Path)
    args = ap.parse_args()
    # Absolute paths: the stand-in and the engine run in their own work folders, where a path
    # relative to this process's folder means nothing (the first CI run lost Mesa that way).
    args.out, args.fso_dir = args.out.resolve(), args.fso_dir.resolve()
    args.mesa_dir = args.mesa_dir.resolve() if args.mesa_dir else None
    args.out.mkdir(parents=True, exist_ok=True)
    w = win32()
    import comtypes.client  # noqa: PLC0415 - Windows-only, see uia_finds
    comtypes.client.GetModule("UIAutomationCore.dll")     # generate the wrapper once, up front
    station = station_name(w)
    hdesk = w.u.CreateDesktopW(DESKTOP_NAME, None, None, 0, GENERIC_ALL, None)
    result = {"window_station": station, "python": sys.version.split()[0],
              "desktop_error": None if hdesk else f"CreateDesktopW error {ctypes.get_last_error()}",
              "results": run_cells(w, args, station, hdesk)}
    if hdesk and not w.u.CloseDesktop(hdesk):
        result["close_desktop_error"] = ctypes.get_last_error()
    (args.out / "probe-hidden-desktop.json").write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
