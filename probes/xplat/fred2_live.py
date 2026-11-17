"""Probe 7: FRED2 with game data, driven with no keyboard, no mouse and no window in front.

Probe 3 on CI (fred2_uia.py) had no game data, so FRED2 never got past error boxes. This one
runs where retail data is installed and answers what the Windows editor design assumes.

Two ways to reach a FRED2 control are measured side by side:

    Windows itself   EnumWindows / EnumChildWindows / GetDlgCtrlID list every window and its
                     control ID, and window messages (WM_COMMAND, WM_SETTEXT, CB_SETCURSEL)
                     act on them. Listing is answered by Windows, not by FRED2.
    UI Automation    the accessibility layer the design names. Its queries are answered by
                     FRED2 itself, and were slow here (seconds for one dialog).

What is recorded:

    ui_roundtrip_ms      how long FRED2 takes to answer one empty window message
    open_mission_ok      File > Open by posted menu command, the path set in the common
                         dialog, its Open button pressed; the title then names the mission
    message_boxes        every question box FRED2 raised on the way, and how it was answered
    ship_editor          the dialog's controls and their IDs against fred2/resource.h, read from
                         Windows (ids_by_windows) and from UI Automation (ids_by_uia, with the
                         seconds that one read took)
    text_*               a text field set by UI Automation / by WM_SETTEXT
    combo_*              ways to pick a combo-box item
    event_tree           the Events editor's tree: items listed and expanded (UI Automation)
    capture_*            PrintWindow of each window, saved as .bmp

For every change, "control_took" is what the control itself then says and "data_took" is what
FRED2's data says: the Ship Editor is closed (FRED2 stores the fields into the mission then),
opened again, and the field read back. A control can show a value the program never took
(probe 2's Qt combo did exactly that).

Start it through hidden_run.py to measure a hidden desktop (--label hidden). Every failed step
keeps its error text in "notes"; later steps still run. Exit 0 whatever it finds.

    python fred2_live.py --out DIR --label normal|hidden --fred2 EXE --game-dir DIR
                         --resource-h FILE --mission FILE [--window-size WxH]
"""
import argparse
import ctypes
import json
import re
import shutil
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from a11y_probe import attempt        # one shared copy (QA CR-7)
import fred2_uia                       # resource_ids(), compare_ids(): probe 3's ID bookkeeping

WINDOW_WAIT_S = 90                     # FRED2 parses every table before its window appears
STEP_WAIT_S = 45
POLL_S = 0.3
SETTLE_S = 0.6
MAX_NODES = 3000
MAX_BOXES = 5
MAX_EXPAND = 10
MAX_NEXT = 12
MSG_TIMEOUT_MS = 20000                 # generous: FRED2 answered in under 20 ms when measured
ROUNDTRIP_SAMPLES = 5

# Menu command IDs (fred2/resource.h and MFC's afxres.h at release 26.2.0)
ID_FILE_OPEN, ID_FILE_SAVE, ID_EDITORS_SHIPS, ID_EDITORS_EVENTS = 57601, 57603, 32799, 32974
# Control IDs (fred2/resource.h)
IDC_SHIP_NAME, IDC_SHIP_CLASS, IDC_AI_CLASS = 1126, 1003, 1194
IDC_EVENT_TREE, IDC_NEXT, IDC_PREV, IDC_WING = 1266, 1373, 1382, 1321
FILE_NAME_BOX, IDOK, IDCANCEL = 1148, 1, 2    # the common Open dialog's file name box; winuser.h
# A Windows message box: the probe answers No, else Cancel, else OK, so a question such as
# "load the autosave instead?" never changes what is being measured.
BOX_ANSWERS = (7, 2, 1)
BOX_TEXT_ID = 65535                    # IDC_STATIC as an unsigned 16-bit ID
BOX_ID_LIMIT = 100                     # a message box has only Windows' own small control IDs
DIALOG_CLASS = "#32770"                # the window class of every Win32 dialog box

WM_NULL, WM_CLOSE, WM_COMMAND = 0x0000, 0x0010, 0x0111
WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH = 0x000C, 0x000D, 0x000E
CB_GETCOUNT, CB_GETCURSEL, CB_GETLBTEXT, CB_GETLBTEXTLEN, CB_SETCURSEL = (
    0x0146, 0x0147, 0x0148, 0x0149, 0x014E)
BN_CLICKED, CBN_SELCHANGE = 0, 1
SMTO_ABORTIFHUNG = 0x0002
SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
PW_RENDERFULLCONTENT = 0x2
UIA_PROPS = (30011, 30003)             # AutomationId, ControlType: read in one call per window
UIA_VALUE, UIA_SELECTION_ITEM, UIA_EXPAND_COLLAPSE = 10002, 10010, 10005
UIA_TREE_ITEM, UIA_LIST_ITEM = 50024, 50007
TREESCOPE_SUBTREE = 7


def wait_value(fn, seconds: float):
    """Call fn() every POLL_S until it returns something truthy or seconds pass; return it."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:        # bounded by the deadline
        value = fn()
        if value:
            return value
        time.sleep(POLL_S)
    return fn()


def bmp_bytes(width: int, height: int, bgra: bytes) -> bytes:
    """Wrap bottom-up 32-bit BGRA pixel rows in a .bmp file header (BITMAPFILEHEADER +
    BITMAPINFOHEADER, 54 bytes) so any image tool can open the capture."""
    header = struct.pack("<2sIHHI", b"BM", 54 + len(bgra), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, 0, len(bgra), 2835, 2835, 0, 0)
    return header + info + bgra


def distinct_colours(bgra: bytes, step: int = 4099) -> int:
    """Count distinct pixel values in a sparse sample of a BGRA buffer. One colour means the
    capture is blank; step is a prime so the sample does not walk down one column."""
    return len({bgra[i:i + 4] for i in range(0, len(bgra) - 3, 4 * step)})


def is_message_box(ids: list) -> bool:
    """True when a dialog's control IDs are those of a Windows message box: only Windows' own
    small positive IDs (buttons 1-11, the icon 20) or the static-text ID 65535, and one of the
    answer buttons. A negative or zero ID means an ordinary dialog (QA TA-5): those must
    never be answered blindly."""
    return bool(ids) and all(0 < i < BOX_ID_LIMIT or i == BOX_TEXT_ID for i in ids) and any(
        i in BOX_ANSWERS for i in ids)


def pick_answer(ids: list):
    """The button the probe presses on a message box: No (7), else Cancel (2), else OK (1), so
    a question never changes what is being measured; None when none of them is there."""
    return next((answer for answer in BOX_ANSWERS if answer in ids), None)


def other_index(state: dict) -> int:
    """An item index different from the selected one (the next, wrapping)."""
    return (max(state["index"], 0) + 1) % max(state["count"], 1)


def parse_size(text: str) -> tuple:
    """"640x480" -> (640, 480)."""
    width, height = text.lower().split("x")
    return int(width), int(height)


def command_wparam(control_id: int, notification: int) -> int:
    """The wParam of a WM_COMMAND from a control: low word its ID, high word what happened."""
    return (notification << 16) | (control_id & 0xFFFF)


def win32() -> SimpleNamespace:
    """user32/gdi32 calls with pointer-sized handle types (ctypes would truncate them to 32
    bits otherwise). Windows only; nothing runs at import."""
    from ctypes import wintypes as wt  # noqa: PLC0415 - Windows-only

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    u = ctypes.WinDLL("user32", use_last_error=True)
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    P = ctypes.POINTER
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    for fn, res, args in (
            (u.PostMessageW, wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
            (u.SendMessageTimeoutW, wt.LPARAM, [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p,
                                                wt.UINT, wt.UINT, P(ctypes.c_size_t)]),
            (u.EnumWindows, wt.BOOL, [enum_proc, wt.LPARAM]),
            (u.EnumChildWindows, wt.BOOL, [wt.HWND, enum_proc, wt.LPARAM]),
            (u.GetWindowThreadProcessId, wt.DWORD, [wt.HWND, P(wt.DWORD)]),
            (u.GetDlgCtrlID, ctypes.c_int, [wt.HWND]),
            (u.GetParent, wt.HWND, [wt.HWND]),
            (u.GetClassNameW, ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
            (u.IsWindowVisible, wt.BOOL, [wt.HWND]),
            (u.IsWindowEnabled, wt.BOOL, [wt.HWND]),
            (u.SetWindowPos, wt.BOOL, [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.UINT]),
            (u.GetWindowRect, wt.BOOL, [wt.HWND, P(wt.RECT)]),
            (u.GetDC, wt.HDC, [wt.HWND]),
            (u.ReleaseDC, ctypes.c_int, [wt.HWND, wt.HDC]),
            (u.PrintWindow, wt.BOOL, [wt.HWND, wt.HDC, wt.UINT]),
            (g.CreateCompatibleDC, wt.HDC, [wt.HDC]),
            (g.CreateCompatibleBitmap, wt.HBITMAP, [wt.HDC, ctypes.c_int, ctypes.c_int]),
            (g.SelectObject, wt.HGDIOBJ, [wt.HDC, wt.HGDIOBJ]),
            (g.GetDIBits, ctypes.c_int, [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                                         ctypes.c_void_p, wt.UINT]),
            (g.DeleteObject, wt.BOOL, [wt.HGDIOBJ]),
            (g.DeleteDC, wt.BOOL, [wt.HDC])):
        fn.restype, fn.argtypes = res, args
    return SimpleNamespace(u=u, g=g, wt=wt, enum_proc=enum_proc,
                           BITMAPINFOHEADER=BITMAPINFOHEADER)


def desktop_name() -> str:
    """The name of the desktop this process runs on ("Default" is the one the user sees). It
    goes into every result, so a "hidden" row proves it was measured on another desktop."""
    from ctypes import wintypes as wt  # noqa: PLC0415 - Windows-only
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.GetThreadDesktop.restype, u.GetThreadDesktop.argtypes = wt.HANDLE, [wt.DWORD]
    u.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                            ctypes.POINTER(wt.DWORD)]
    buf = ctypes.create_unicode_buffer(256)
    need = wt.DWORD()
    UOI_NAME = 2
    if not u.GetUserObjectInformationW(u.GetThreadDesktop(k.GetCurrentThreadId()), UOI_NAME, buf,
                                       ctypes.sizeof(buf), ctypes.byref(need)):
        return f"? (error {ctypes.get_last_error()})"
    return buf.value


# ---- What Windows itself knows (no message to FRED2, so no waiting for a frame) ----

def class_name(w, hwnd: int) -> str:
    """A window's class name, as Windows itself reports it."""
    buf = ctypes.create_unicode_buffer(256)
    w.u.GetClassNameW(hwnd, buf, 256)
    return buf.value


def top_windows(w, pid: int) -> list:
    """Handles of the visible top-level windows of process pid. A dialog owned by the main
    window is a top-level window too (owned is not the same as child)."""
    found = []

    def visit(hwnd, _):
        """EnumWindows callback: keep the visible windows of this process."""
        owner = w.wt.DWORD()
        w.u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and w.u.IsWindowVisible(hwnd):
            found.append(hwnd)
        return len(found) < MAX_NODES             # False stops the enumeration: bounded

    w.u.EnumWindows(w.enum_proc(visit), 0)
    return found


def controls(w, hwnd: int) -> list:
    """Every control under the window hwnd: handle, control ID, class, enabled, visible."""
    found = []

    def visit(child, _):
        """EnumChildWindows callback: record one control."""
        found.append(SimpleNamespace(hwnd=child, id=w.u.GetDlgCtrlID(child),
                                     cls=class_name(w, child),
                                     enabled=bool(w.u.IsWindowEnabled(child)),
                                     visible=bool(w.u.IsWindowVisible(child))))
        return len(found) < MAX_NODES             # bounded

    w.u.EnumChildWindows(hwnd, w.enum_proc(visit), 0)
    return found


def control(w, dialog: int, control_id: int, cls: str | None = None):
    """The control with that ID (and class, when given) under dialog, or None. The LAST match:
    a combo box and the edit inside it can share an ID, and the inner one is listed later."""
    hits = [c for c in controls(w, dialog) if c.id == control_id and (cls is None or c.cls == cls)]
    return hits[-1] if hits else None


def find_dialog(ctx, control_id: int):
    """The handle of the visible dialog box that holds a control with control_id, or None."""
    for hwnd in top_windows(ctx.w, ctx.pid):
        if class_name(ctx.w, hwnd) == DIALOG_CLASS and control(ctx.w, hwnd, control_id):
            return hwnd
    return None


# ---- Window messages (each one is answered by FRED2's thread) ----

def send(w, hwnd: int, msg: int, wparam: int = 0, lparam=None) -> int:
    """SendMessage with a time limit, so a busy or modal FRED2 cannot hang the probe. Returns
    the message's result; raises TimeoutError when the window did not answer in time."""
    result = ctypes.c_size_t()
    if not w.u.SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, MSG_TIMEOUT_MS,
                                   ctypes.byref(result)):
        raise TimeoutError(f"window {hwnd:#x} did not answer message {msg:#x}")
    # CB_ERR is -1 as a pointer-sized value; hand it back as -1
    return -1 if result.value == ctypes.c_size_t(-1).value else int(result.value)


def press(w, dialog: int, control_id: int) -> None:
    """What a click on a dialog's button sends: WM_COMMAND with BN_CLICKED to the dialog.
    Posted, so it returns at once even when the button opens a modal box."""
    button = control(w, dialog, control_id)
    w.u.PostMessageW(dialog, WM_COMMAND, command_wparam(control_id, BN_CLICKED),
                     button.hwnd if button else 0)


def roundtrip_ms(w, hwnd: int) -> float:
    """Median time for hwnd's thread to answer an empty message (WM_NULL)."""
    times = []
    for _ in range(ROUNDTRIP_SAMPLES):
        start = time.perf_counter()
        send(w, hwnd, WM_NULL)
        times.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(times), 1)


def window_text(w, hwnd: int) -> str:
    """The text Windows holds for hwnd (WM_GETTEXT), independent of UI Automation."""
    length = send(w, hwnd, WM_GETTEXTLENGTH)
    buf = ctypes.create_unicode_buffer(length + 1)
    send(w, hwnd, WM_GETTEXT, length + 1, ctypes.cast(buf, ctypes.c_void_p))
    return buf.value


def set_text(w, hwnd: int, text: str) -> None:
    """Set a control's text with WM_SETTEXT (FRED2's data took a ship name set this way)."""
    buf = ctypes.create_unicode_buffer(text)
    send(w, hwnd, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p))


def combo_state(w, hwnd: int) -> dict:
    """A Win32 combo box's item count, selected index and selected text, asked of the control."""
    index = send(w, hwnd, CB_GETCURSEL)
    text = None
    if index >= 0:
        buf = ctypes.create_unicode_buffer(send(w, hwnd, CB_GETLBTEXTLEN, index) + 1)
        send(w, hwnd, CB_GETLBTEXT, index, ctypes.cast(buf, ctypes.c_void_p))
        text = buf.value
    return {"count": send(w, hwnd, CB_GETCOUNT), "index": index, "text": text}


def capture(w, hwnd: int, path: Path) -> dict:
    """PrintWindow hwnd (whole window, GPU-drawn content included) into path as a .bmp; return
    its size and how many distinct colours a sparse sample holds."""
    rect = w.wt.RECT()
    if not w.u.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    width, height = rect.right - rect.left, rect.bottom - rect.top
    hdc = w.u.GetDC(hwnd)
    if not hdc:
        raise ctypes.WinError(ctypes.get_last_error())
    mem = w.g.CreateCompatibleDC(hdc)
    bmp = w.g.CreateCompatibleBitmap(hdc, width, height)
    if not mem or not bmp:
        raise RuntimeError("no memory device context or bitmap for the capture")
    old = w.g.SelectObject(mem, bmp)
    try:
        printed = bool(w.u.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT))
        w.g.SelectObject(mem, old)             # GetDIBits wants the bitmap out of any DC
        info = w.BITMAPINFOHEADER(biSize=40, biWidth=width, biHeight=height, biPlanes=1,
                                  biBitCount=32)
        pixels = ctypes.create_string_buffer(width * height * 4)
        rows = w.g.GetDIBits(mem, bmp, 0, height, pixels, ctypes.byref(info), 0)
    finally:
        w.g.DeleteObject(bmp)
        w.g.DeleteDC(mem)
        w.u.ReleaseDC(hwnd, hdc)
    if rows != height:
        raise RuntimeError(f"GetDIBits returned {rows} of {height} rows")
    path.write_bytes(bmp_bytes(width, height, pixels.raw))
    return {"printwindow_ok": printed, "width": width, "height": height,
            "distinct_colours": distinct_colours(pixels.raw), "file": path.name}


# ---- UI Automation (comtypes): only for what is being compared, and for the tree ----

class Uia:
    """The few UI Automation calls this probe makes. Elements are always reached from a window
    handle (ElementFromHandle), never by searching from the desktop's root: the first version
    searched from the root and spent most of a minute finding FRED2's windows."""

    def __init__(self):
        """Start UI Automation and a cache request for the two properties read in bulk."""
        import comtypes.client  # noqa: PLC0415 - Windows-only package
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uiac  # noqa: PLC0415 - generated above
        self.c = uiac
        self.uia = comtypes.client.CreateObject(uiac.CUIAutomation, interface=uiac.IUIAutomation)
        self.cache = self.uia.CreateCacheRequest()
        for prop in UIA_PROPS:
            self.cache.AddProperty(prop)

    def element(self, hwnd: int):
        """The UI Automation element for a window handle."""
        return self.uia.ElementFromHandle(hwnd)

    def subtree(self, element) -> list:
        """(element, AutomationId, ControlType) for element and everything under it, the two
        properties read in the same call as the search."""
        found = element.FindAllBuildCache(TREESCOPE_SUBTREE, self.uia.CreateTrueCondition(),
                                          self.cache)
        rows = []
        for i in range(min(found.Length, MAX_NODES)):
            e = found.GetElement(i)
            rows.append((e, e.CachedAutomationId, e.CachedControlType))
        return rows

    def pattern(self, element, pattern_id: int, iface):
        """One of an element's control patterns, as the given interface."""
        return element.GetCurrentPattern(pattern_id).QueryInterface(iface)

    def set_value(self, element, text: str) -> None:
        """Set an element's value (ValuePattern)."""
        self.pattern(element, UIA_VALUE, self.c.IUIAutomationValuePattern).SetValue(text)

    def expand(self, element) -> None:
        """Open an element's list or subtree (ExpandCollapsePattern)."""
        self.pattern(element, UIA_EXPAND_COLLAPSE,
                     self.c.IUIAutomationExpandCollapsePattern).Expand()

    def collapse(self, element) -> None:
        """Close an element's list or subtree (ExpandCollapsePattern)."""
        self.pattern(element, UIA_EXPAND_COLLAPSE,
                     self.c.IUIAutomationExpandCollapsePattern).Collapse()

    def select(self, element) -> None:
        """Select an element (SelectionItemPattern)."""
        self.pattern(element, UIA_SELECTION_ITEM,
                     self.c.IUIAutomationSelectionItemPattern).Select()


# ---- The steps ----

def answer_boxes(ctx) -> None:
    """Record each message box FRED2 shows and answer it (No, else Cancel, else OK), at most
    MAX_BOXES times. FRED2 asks its questions in modal boxes; until one is answered nothing
    else in the editor responds, so an editor driver has to see and answer them."""
    w = ctx.w
    for _ in range(MAX_BOXES):                    # bounded
        box = next((h for h in top_windows(w, ctx.pid) if class_name(w, h) == DIALOG_CLASS
                    and is_message_box([c.id for c in controls(w, h)])), None)
        if box is None:
            return
        parts = controls(w, box)
        text = [window_text(w, c.hwnd) for c in parts if c.id == BOX_TEXT_ID]
        answer = pick_answer([c.id for c in parts])
        button = control(w, box, answer)
        ctx.boxes.append({"title": window_text(w, box), "text": [t for t in text if t],
                          "answered": window_text(w, button.hwnd)})
        press(w, box, answer)
        time.sleep(SETTLE_S * 2)


def open_mission(ctx, mission: Path) -> bool:
    """File > Open by posted menu command, the path set in the common dialog's file name box,
    its Open button pressed; True when the main window's title then names the mission."""
    w, notes = ctx.w, ctx.notes
    w.u.PostMessageW(ctx.main, WM_COMMAND, ID_FILE_OPEN, 0)
    dialog = wait_value(lambda: find_dialog(ctx, FILE_NAME_BOX), STEP_WAIT_S)
    if not dialog:
        notes.append("no Open dialog with a file name box appeared")
        return False
    # The file name box is a combo holding an edit, both with ID 1148; the edit takes the text
    edit = control(w, dialog, FILE_NAME_BOX, "Edit")
    attempt(notes, "set file name", lambda: set_text(w, edit.hwnd, str(mission)))
    time.sleep(SETTLE_S)
    press(w, dialog, IDOK)

    def loaded():
        """Answer any box, then say whether the title names the mission."""
        attempt(notes, "answer boxes", lambda: answer_boxes(ctx))
        return mission.name.lower() in window_text(w, ctx.main).lower()

    opened = wait_value(loaded, STEP_WAIT_S)
    notes.append(f"main window title after Open: {window_text(w, ctx.main)!r}")
    return bool(opened)


def open_ship_editor(ctx):
    """Return the Ship Editor's dialog handle, posting the Editors > Ships menu command first
    when it is not showing (FRED2 can start with it open: it remembers that in the registry)."""
    if not find_dialog(ctx, IDC_SHIP_NAME):
        ctx.w.u.PostMessageW(ctx.main, WM_COMMAND, ID_EDITORS_SHIPS, 0)
    return wait_value(lambda: find_dialog(ctx, IDC_SHIP_NAME), STEP_WAIT_S)


def ship_name(ctx, editor: int) -> str:
    """The text in the Ship Editor's name field."""
    return window_text(ctx.w, control(ctx.w, editor, IDC_SHIP_NAME).hwnd)


def settle(ctx) -> None:
    """Give FRED2 a moment after a change of ship, then answer any box it raised. WHY: a second
    Next pressed straight after the first made FRED2 raise "Number of waves illegal" boxes
    (the 18:00 run on 2026-11-16; with a 2 s pause between presses the same steps raised
    none), and a box left up blocks every later step."""
    time.sleep(SETTLE_S * 3)
    attempt(ctx.notes, "answer boxes", lambda: answer_boxes(ctx))


def refill_from_data(ctx, editor: int) -> dict:
    """Make the Ship Editor store its fields and then show what FRED2's mission data holds.

    WHY not close and reopen (the first version): closing only hides the dialog and reopening
    only shows it (fred2/shipeditordlg.cpp OnClose, fredview.cpp OnEditorsShips at 26.2.0), so
    the controls kept what the probe had put in them and "data took" was the control read
    twice (QA CR-1/TA-1). The dialog's Next button stores the fields (update_data), marks
    another ship and fills every control from the mission data (initialize_data); Prev does
    the same back to the first ship (OnNext/OnPrev, same file). So after Next then Prev, each
    control shows the stored data, and the other ship's name in between proves the controls
    really were refilled. Returns {"other_ship": its name}; raises when either step shows no
    change of ship."""
    w = ctx.w
    here = ship_name(ctx, editor)
    press(w, editor, IDC_NEXT)
    other = wait_value(lambda: (n := ship_name(ctx, editor)) != here and n, STEP_WAIT_S)
    settle(ctx)
    if not other:
        raise LookupError("Next did not move to another ship (the fields were refused?)")
    press(w, editor, IDC_PREV)
    # back on the ship we started from, by name: "not the other ship" would also accept a
    # third ship, whose fields would then be read as the test ship's (QA SC-3/ST-3)
    if not wait_value(lambda: ship_name(ctx, editor) == here, STEP_WAIT_S):
        raise LookupError("Prev did not come back to the ship it started from")
    settle(ctx)
    return {"other_ship": other}


def saved_ship(text: str, name: str) -> dict | None:
    """What a saved mission file says about the ship called name: its class and AI class, or
    None when the file has no such ship. A ship entry is "$Name: <name>" (FRED2 adds a
    ";! Object #n" remark on that line) with "$Class: ..." on the next line and, when it is
    not the default, "+AI Class: ..." further down. Other sections (messages, wings) also
    have "$Name:" lines, so only an entry whose next line is $Class counts. A turret with its
    own AI class is saved as "+AI Class:" under a "+Subsystem:" line inside the same entry
    (missionsave.cpp save_turret_info), so the ship's own is looked for only above the first
    "+Subsystem:" (QA ST-2)."""
    # [^\n] rather than "." for the two one-line parts: DOTALL (needed for the entry's body)
    # let "." run on into the next ship's lines, and the first version returned that ship.
    entry = re.compile(rf"^\$Name:[ \t]*{re.escape(name)}[ \t]*(?:;[^\n]*)?\n"
                       r"\$Class:[ \t]*([^\n]+?)[ \t]*\n(.*?)(?=^\$Name:|\Z)",
                       re.MULTILINE | re.DOTALL)
    found = entry.search(text)
    if not found:
        return None
    own = re.split(r"^\+Subsystem:", found.group(2), maxsplit=1, flags=re.MULTILINE)[0]
    ai = re.search(r"^\+AI Class:[ \t]*(.+?)[ \t]*$", own, re.MULTILINE)
    return {"class": found.group(1), "ai_class": ai.group(1) if ai else None}


def save_and_read(ctx, mission: Path, name: str, r: dict) -> None:
    """A second witness that does not involve the dialog at all: File > Save (posted menu
    command), then read the probe's own copy of the mission from disk."""
    before = mission.stat().st_mtime_ns
    ctx.w.u.PostMessageW(ctx.main, WM_COMMAND, ID_FILE_SAVE, 0)

    def written():
        """Answer any box, then say whether the file on disk has changed."""
        attempt(ctx.notes, "answer boxes", lambda: answer_boxes(ctx))
        return mission.stat().st_mtime_ns != before

    saved = bool(wait_value(written, STEP_WAIT_S))
    row = {"file_written": saved, "ship": None}
    r["saved_file"] = row
    if saved:
        time.sleep(SETTLE_S)                      # let FRED2 finish writing
        row["ship"] = saved_ship(mission.read_text(errors="replace"), name)


def ids_check(ctx, editor: int, r: dict) -> None:
    """The Ship Editor's control IDs against resource.h, from Windows and from UI Automation."""
    parts = controls(ctx.w, editor)
    rows = [{"AutomationId": str(c.id), "Name": "", "ClassName": c.cls, "Enabled": c.enabled}
            for c in parts]
    verdict, buckets, _ = fred2_uia.compare_ids(rows, ctx.ids)
    r["ship_editor"] = {"title": window_text(ctx.w, editor), "controls": len(parts),
                        "enabled_controls": sum(c.enabled for c in parts),
                        "ids_by_windows": {"all_known": verdict, "counts": {
                            k: len(v) for k, v in buckets.items()},
                            "other": sorted(set(buckets["other"]))}}
    ctx.dump["ship_editor_controls"] = rows
    start = time.monotonic()
    tree = attempt(ctx.notes, "UIA read of the Ship Editor",
                   lambda: ctx.uia.subtree(ctx.uia.element(editor)))
    seconds = round(time.monotonic() - start, 1)
    if tree is None:
        return
    uia_rows = [{"AutomationId": ident, "Name": "", "ControlType": kind} for _, ident, kind in tree]
    verdict, buckets, _ = fred2_uia.compare_ids(uia_rows, ctx.ids)
    by_windows = {c.id for c in parts if c.id > 0}
    by_uia = {n for n in (fred2_uia.numeric_id(i) for _, i, _ in tree) if n is not None and n > 0}
    r["ship_editor"]["ids_by_uia"] = {
        "seconds": seconds, "elements": len(tree), "all_known": verdict,
        "counts": {k: len(v) for k, v in buckets.items()},
        "other": sorted(set(buckets["other"])),
        # do the two views name the same controls?
        "ids_only_windows_sees": sorted(by_windows - by_uia)[:20],
        "ids_only_uia_sees": sorted(by_uia - by_windows)[:20]}


def text_routes(ctx, editor: int, r: dict) -> None:
    """Set the ship's name two ways; judge each by the control and by FRED2's data."""
    w, notes = ctx.w, ctx.notes

    def by_uia(hwnd, text):
        """Set the text through UI Automation's ValuePattern."""
        ctx.uia.set_value(ctx.uia.element(hwnd), text)

    def by_message(hwnd, text):
        """Set the text with WM_SETTEXT."""
        set_text(w, hwnd, text)

    for key, route, new_name in (("text_uia_setvalue", by_uia, "Probe 7a"),
                                 ("text_wm_settext", by_message, "Probe 7b")):
        row = {"control_took": False, "data_took": False, "error": None}
        r[key] = row
        boxes_before = len(ctx.boxes)
        name = control(w, editor, IDC_SHIP_NAME)
        row["enabled"] = name.enabled
        row["before"] = attempt(notes, f"{key} before", lambda n=name: window_text(w, n.hwnd))
        try:
            route(name.hwnd, new_name)
        except Exception as e:     # a measurement: recorded in the row, never raised
            row["error"] = f"{type(e).__name__}: {e}"
        time.sleep(SETTLE_S)
        row["after"] = attempt(notes, f"{key} after", lambda n=name: window_text(w, n.hwnd))
        row["control_took"] = row["after"] == new_name
        row["refill"] = attempt(notes, f"{key} refill", lambda: refill_from_data(ctx, editor))
        if row["refill"]:
            row["after_refill"] = attempt(notes, f"{key} stored", lambda: ship_name(ctx, editor))
            row["data_took"] = row["after_refill"] == new_name
        row["boxes"] = [b["text"] for b in ctx.boxes[boxes_before:]]
        ctx.ship = attempt(notes, "ship name", lambda: ship_name(ctx, editor)) or ctx.ship


def uia_select(ctx, combo, target: int, row: dict) -> None:
    """UI Automation's way to pick a combo item: expand, Select the item, collapse. The control
    is read before the collapse as well as after (row["before_collapse"]), and a collapse
    error is kept, so "Select did not take" can be told from "Collapse undid it" (QA CR-5)."""
    uia = ctx.uia
    element = uia.element(combo.hwnd)
    uia.expand(element)
    try:
        time.sleep(SETTLE_S)
        items = [e for e, _, kind in uia.subtree(element) if kind == UIA_LIST_ITEM]
        row["list_items_seen"] = len(items)
        uia.select(items[target])
        time.sleep(SETTLE_S)
        row["before_collapse"] = combo_state(ctx.w, combo.hwnd)
    finally:                                      # never leave the list open for later steps
        row["collapse_error"] = None
        try:
            uia.collapse(element)
        except Exception as e:     # recorded: part of the measurement
            row["collapse_error"] = f"{type(e).__name__}: {e}"


def message_select(ctx, combo, target: int, _row: dict) -> None:
    """CB_SETCURSEL alone: changes the control and tells nobody."""
    send(ctx.w, combo.hwnd, CB_SETCURSEL, target)


def message_select_notify(ctx, combo, target: int, _row: dict) -> None:
    """CB_SETCURSEL, then what a user's pick also sends the dialog: CBN_SELCHANGE (in
    WM_COMMAND: low word the control ID, high word the notification)."""
    w = ctx.w
    send(w, combo.hwnd, CB_SETCURSEL, target)
    if not w.u.PostMessageW(w.u.GetParent(combo.hwnd), WM_COMMAND,
                            command_wparam(combo.id, CBN_SELCHANGE), combo.hwnd):
        raise ctypes.WinError(ctypes.get_last_error())


COMBO_ROUTES = (("combo_ai_uia_select", IDC_AI_CLASS, uia_select),
                ("combo_ai_message", IDC_AI_CLASS, message_select),
                ("combo_ai_message_notify", IDC_AI_CLASS, message_select_notify),
                ("combo_class_uia_select", IDC_SHIP_CLASS, uia_select),
                ("combo_class_message", IDC_SHIP_CLASS, message_select),
                ("combo_class_message_notify", IDC_SHIP_CLASS, message_select_notify))


def combo_routes(ctx, editor: int, r: dict) -> None:
    """Pick a different combo item several ways; judge each by the control and by FRED2's data
    (refill_from_data). A route that raises keeps its error in its own row."""
    w, notes = ctx.w, ctx.notes
    for key, control_id, route in COMBO_ROUTES:
        row = {"control_id": control_id, "control_took": False, "data_took": False, "error": None}
        r[key] = row
        boxes_before = len(ctx.boxes)
        combo = control(w, editor, control_id, "ComboBox")
        if combo is None:
            row["error"] = f"combo {control_id} not found"
            continue
        row["enabled"] = combo.enabled
        before = attempt(notes, f"{key} before", lambda c=combo: combo_state(w, c.hwnd))
        if not before or before["count"] < 2:
            row["error"] = f"combo state {before}; needs two items"
            continue
        target = other_index(before)
        row["before"], row["target_index"] = before, target
        try:
            route(ctx, combo, target, row)
        except Exception as e:     # a measurement: recorded in the row, never raised
            row["error"] = f"{type(e).__name__}: {e}"
        time.sleep(SETTLE_S)
        attempt(notes, "answer boxes", lambda: answer_boxes(ctx))
        row["after"] = attempt(notes, f"{key} after", lambda c=combo: combo_state(w, c.hwnd))
        row["control_took"] = bool(row["after"]) and row["after"]["index"] == target
        row["refill"] = attempt(notes, f"{key} refill", lambda: refill_from_data(ctx, editor))
        if row["refill"]:
            stored = attempt(notes, f"{key} stored", lambda c=combo: combo_state(w, c.hwnd))
            row["after_refill"] = stored
            row["data_took"] = bool(stored) and stored["index"] == target
        row["boxes"] = [b["text"] for b in ctx.boxes[boxes_before:]]


def mark_test_ship(ctx, editor: int) -> dict:
    """Mark a ship that is not in a wing and whose AI Class field is enabled, using the
    dialog's own Next button (no click in the 3D view). With no ship marked every field is
    greyed out; the first ship of this mission is a player ship, whose AI Class stays disabled
    and is never filled from the mission (the first runs measured that one by mistake, QA
    CR-1); and FRED2 refuses to rename a ship that is in a wing ("This ship is part of a wing,
    and its name cannot be changed", the 18:00 run). At most MAX_NEXT presses."""
    w = ctx.w
    for presses in range(1, MAX_NEXT + 1):        # bounded
        before = ship_name(ctx, editor)
        press(w, editor, IDC_NEXT)
        wait_value(lambda b=before: ship_name(ctx, editor) != b, STEP_WAIT_S)
        settle(ctx)
        ai = control(w, editor, IDC_AI_CLASS, "ComboBox")
        wing = control(w, editor, IDC_WING)
        wing_name = window_text(w, wing.hwnd) if wing else None
        if (ai is not None and ai.enabled and control(w, editor, IDC_SHIP_NAME).enabled
                and wing_name == "None"):
            return {"name": ship_name(ctx, editor), "next_presses": presses, "wing": wing_name,
                    "ai_class": combo_state(w, ai.hwnd)}
    raise LookupError(f"no wingless ship with an enabled AI Class in {MAX_NEXT} presses of Next")


def ship_editor_checks(ctx, r: dict) -> None:
    """Open the Ship Editor, mark a ship, then the ID, text and combo checks."""
    w, notes = ctx.w, ctx.notes
    editor = open_ship_editor(ctx)
    if not editor:
        notes.append("the Ship Editor did not appear (no dialog holding control 1126)")
        return
    r["test_ship"] = attempt(notes, "mark a ship", lambda: mark_test_ship(ctx, editor))
    if not r["test_ship"]:
        return
    ctx.ship = r["test_ship"]["name"]
    timed(r, "ids_check", lambda: ids_check(ctx, editor, r))
    attempt(notes, "capture ship editor", lambda: r.__setitem__("capture_ship_editor", capture(
        w, editor, ctx.out / f"fred2-{ctx.label}-ship-editor.bmp")))
    timed(r, "text_routes", lambda: text_routes(ctx, editor, r))
    timed(r, "combo_routes", lambda: combo_routes(ctx, editor, r))
    w.u.PostMessageW(editor, WM_CLOSE, 0, 0)      # hides the dialog; FRED2 stores the fields
    time.sleep(SETTLE_S)
    attempt(notes, "answer boxes", lambda: answer_boxes(ctx))
    timed(r, "save_and_read", lambda: save_and_read(ctx, ctx.mission, ctx.ship, r))


def event_tree_checks(ctx, r: dict) -> None:
    """Open the Events editor, list its tree items, expand the first few that can expand."""
    uia, w, notes = ctx.uia, ctx.w, ctx.notes
    w.u.PostMessageW(ctx.main, WM_COMMAND, ID_EDITORS_EVENTS, 0)
    editor = wait_value(lambda: find_dialog(ctx, IDC_EVENT_TREE), STEP_WAIT_S)
    row = {"opened": bool(editor), "items": 0, "items_after_expand": 0}
    r["event_tree"] = row
    if not editor:
        notes.append("the Events editor did not appear (no dialog holding control 1266)")
        return
    tree = control(w, editor, IDC_EVENT_TREE)
    row["tree_class"] = tree.cls
    element = uia.element(tree.hwnd)

    def items():
        """The tree's items as UI Automation lists them now."""
        return [e for e, _, kind in uia.subtree(element) if kind == UIA_TREE_ITEM]

    start = time.monotonic()
    first = attempt(notes, "tree items", items) or []
    row["list_seconds"] = round(time.monotonic() - start, 1)
    row["items"] = len(first)
    row["first_names"] = [attempt(notes, "item name", lambda e=e: e.CurrentName)
                          for e in first[:5]]
    expanded = 0
    for item in first[:MAX_EXPAND]:               # bounded: the first few are enough
        if attempt(row.setdefault("expand_errors", []), "expand",
                   lambda i=item: uia.expand(i) or True):
            expanded += 1
    row["expanded"] = expanded
    time.sleep(SETTLE_S)
    after = attempt(notes, "tree items after expand", items) or []
    row["items_after_expand"] = len(after)
    row["names_after_expand"] = [attempt(notes, "item name", lambda e=e: e.CurrentName)
                                 for e in after[:8]]
    attempt(notes, "capture events editor", lambda: r.__setitem__("capture_events", capture(
        w, editor, ctx.out / f"fred2-{ctx.label}-events.bmp")))
    press(w, editor, IDCANCEL)
    time.sleep(SETTLE_S)
    attempt(notes, "answer boxes", lambda: answer_boxes(ctx))


def timed(r: dict, name: str, fn) -> None:
    """Run one step and record how long it took under r["seconds"]."""
    start = time.monotonic()
    attempt(r["notes"], name, fn)
    r["seconds"][name] = round(time.monotonic() - start, 1)


def find_main(ctx_w, pid: int):
    """FRED2's main window: the MFC frame (class Afx:...) among the process's windows."""
    return next((h for h in top_windows(ctx_w, pid) if class_name(ctx_w, h).startswith("Afx:")),
                None)


def run_checks(args, out: Path, proc, r: dict, dump: dict) -> None:
    """Everything after FRED2 has been started."""
    w = win32()
    start = time.monotonic()
    main = wait_value(lambda: find_main(w, proc.pid), WINDOW_WAIT_S)
    r["seconds"]["start_to_main_window"] = round(time.monotonic() - start, 1)
    if not main:
        r["notes"].append(f"no FRED2 main window in {WINDOW_WAIT_S} s; windows: "
                          f"{[class_name(w, h) for h in top_windows(w, proc.pid)]}")
        return
    uia = attempt(r["notes"], "UI Automation", Uia)
    if uia is None:
        return
    r["main_window"], r["message_boxes"] = True, []
    r["desktop"] = attempt(r["notes"], "desktop name", desktop_name)
    ctx = SimpleNamespace(uia=uia, w=w, notes=r["notes"], pid=proc.pid, main=main,
                          boxes=r["message_boxes"], out=out, dump=dump, label=args.label,
                          ship=None, mission=out / f"probe7-{args.label}.fs2",
                          ids=fred2_uia.resource_ids(args.resource_h.read_text()))
    attempt(r["notes"], "answer boxes", lambda: answer_boxes(ctx))
    r["main_title"] = attempt(r["notes"], "main title", lambda: window_text(w, main))
    r["main_class"] = class_name(w, main)
    if args.window_size:
        # Optional: a smaller 3D view is less work per frame for a software renderer.
        width, height = parse_size(args.window_size)
        w.u.SetWindowPos(main, None, 0, 0, width, height,
                         SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
    r["ui_roundtrip_ms"] = {"empty_mission": attempt(r["notes"], "roundtrip",
                                                     lambda: roundtrip_ms(w, main))}
    # WHY a copy: the probe changes the mission in FRED2 (never saved, but FRED2 autosaves), so
    # it works on a copy of its own; shutil.copy gives the copy a fresh modification time, so
    # no older autosave looks newer than it.
    mission = ctx.mission
    shutil.copy(args.mission.resolve(), mission)
    timed(r, "open_mission", lambda: r.__setitem__("open_mission_ok", open_mission(ctx, mission)))
    r["ui_roundtrip_ms"]["mission_loaded"] = attempt(r["notes"], "roundtrip",
                                                     lambda: roundtrip_ms(w, main))
    timed(r, "ship_editor", lambda: ship_editor_checks(ctx, r))
    timed(r, "event_tree", lambda: event_tree_checks(ctx, r))
    timed(r, "capture_main", lambda: r.__setitem__("capture_main", capture(
        w, main, out / f"fred2-{args.label}-main.bmp")))


def main() -> int:
    """Start FRED2, run every check, write probe-fred2-live-<label>.json; always exit 0."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--label", choices=("normal", "hidden"), required=True)
    ap.add_argument("--fred2", type=Path, required=True)
    ap.add_argument("--game-dir", type=Path, required=True)
    ap.add_argument("--resource-h", type=Path, required=True)
    ap.add_argument("--mission", type=Path, required=True)
    ap.add_argument("--window-size", help="resize FRED2's main window, e.g. 640x480")
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    r = {"label": args.label, "fred2": args.fred2.name, "window_size": args.window_size,
         "main_window": False, "open_mission_ok": False, "seconds": {}, "notes": []}
    dump: dict = {}
    proc = subprocess.Popen([str(args.fred2.resolve()), "-nosound"], cwd=args.game_dir.resolve())
    try:
        # attempt(): the probe exits 0 whatever happens inside (QA CR-8)
        attempt(r["notes"], "run_checks", lambda: run_checks(args, out, proc, r, dump))
    finally:
        proc.kill()                               # FRED2 never outlives the probe
        proc.wait()
        # written in every case, so a failure in a step still leaves what was measured
        (out / f"probe-fred2-live-{args.label}.json").write_text(json.dumps(r, indent=1))
        (out / f"probe-fred2-live-{args.label}-controls.json").write_text(
            json.dumps(dump, indent=1))
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
