"""Probe 8: ways to pick a Qt combo-box item when the accessibility "select" does not take.

Probe 2 found that on every OS the accessibility layer's own selection calls report success on
a Qt combo box while Qt keeps the old item. The candidate fix was "give the combo keyboard
focus through accessibility, then choose the item with keys". This probe measures that and
its neighbours on the same stand-in dialog (qt_standin.py), one route at a time:

    before each route   a stand-in of its own is started (the combo is on its first item;
                        starts_at records it)
    the route           tries to select the SECOND item
    took                Qt's own state.json then shows index 1 (not what any accessibility
                        property says: probe 2's lesson)

Routes differ in how focus gets there (accessibility call, click, none) and how the choice is
made (posted key messages, real input events, a click on the item). The ones that use no real
input are the ones a hidden Windows session can use; start this script through hidden_run.py
to measure exactly that (--label hidden).

Every route is tried even when an earlier one worked; an exception inside a route is recorded
in its row. Exit 0 whatever is found: a measurement.

    python qt_combo_routes.py --out DIR [--label NAME] [--standin-python PATH]

Linux: run inside probes/xplat/linux_a11y_session.sh (Xvfb, AT-SPI bus, no window manager).
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import a11y_probe as ap                # readers and the stand-in launcher of probe 2

TARGET_INDEX = 1
TARGET = ap.NEW_CLASS                  # the second item's text
DIALOG = "standinDialog"
ROUTE_WAIT_S = 3
END_SETTLE_S = 0.5      # after the target is first seen: time for a late key to move it on
KEY_GAP_S = 0.15
SETTLE_S = 0.5


def read_state(out: Path):
    """What Qt itself last recorded (index, ship_class, focus), or None."""
    try:
        return json.loads((out / "state.json").read_text())
    except (OSError, ValueError):
        return None


def run_route(name: str, fn, env, rows: list) -> None:
    """Run one route against a fresh stand-in (the combo starts on its first item) and record
    whether Qt ended on the target item. starts_at is Qt's index before the route; a route
    may add its own fields to env.row."""
    row = {"route": name, "took": False, "starts_at": (read_state(env.out) or {}).get("index"),
           "error": None}
    env.row = row
    rows.append(row)
    try:
        fn(env)
    except Exception as e:     # a measurement: any failure is recorded, never raised
        row["error"] = f"{type(e).__name__}: {e}"
    seen = ap.wait_for(
        lambda: (read_state(env.out) or {}).get("index") == TARGET_INDEX, ROUTE_WAIT_S)
    time.sleep(END_SETTLE_S)
    state = read_state(env.out) or {}
    row["qt_index"], row["qt_focus"] = state.get("index"), state.get("focus")
    # WHY all three (QA ST-1): seeing the target once is not ending on it (two keys handled
    # late pass through it), and a route that started on the target did nothing.
    row["took"] = bool(seen and row["qt_index"] == TARGET_INDEX and row["starts_at"] == 0)


def node_named(env, object_name: str):
    """The current accessible node whose id names object_name (walked fresh: ids of popups and
    their items exist only while the popup is open)."""
    node = ap.find_by_id(env.reader.nodes(env.pid), object_name)
    if node is None:
        raise LookupError(f"no accessible element for {object_name}")
    return node


class WindowsRoutes:
    """Windows: UI Automation for focus and finding; window messages or real keys for input."""

    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON = 0x0200, 0x0201, 0x0202, 0x0001
    GA_ROOT, KEYEVENTF_KEYUP, LEGACY_PATTERN = 2, 0x0002, 10018
    DOWN, RETURN = (0x28, 0x50, True), (0x0D, 0x1C, False)     # (virtual key, scan code, extended)

    def __init__(self):
        """Load user32 with the signatures the routes call, and the UI Automation interfaces."""
        import ctypes  # noqa: PLC0415 - Windows-only calls below
        from ctypes import wintypes as wt  # noqa: PLC0415

        from comtypes.gen import UIAutomationClient as uiac  # noqa: PLC0415 - made by the reader
        self.ctypes, self.wt, self.uiac = ctypes, wt, uiac
        self.u = u = ctypes.WinDLL("user32", use_last_error=True)
        for fn, res, args in (
                (u.PostMessageW, wt.BOOL, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]),
                (u.GetAncestor, wt.HWND, [wt.HWND, wt.UINT]),
                (u.ScreenToClient, wt.BOOL, [wt.HWND, ctypes.POINTER(wt.POINT)]),
                (u.SetForegroundWindow, wt.BOOL, [wt.HWND]),
                (u.keybd_event, None, [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p])):
            fn.restype, fn.argtypes = res, args

    def post_key(self, hwnd, key) -> None:
        """Post a key press and release to hwnd's message queue: no real input, so it works
        whatever desktop or window has the user's keyboard."""
        vk, scan, extended = key
        down = 1 | (scan << 16) | ((1 << 24) if extended else 0)
        if not (self.u.PostMessageW(hwnd, self.WM_KEYDOWN, vk, down)
                and self.u.PostMessageW(hwnd, self.WM_KEYUP, vk, down | (1 << 30) | (1 << 31))):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        time.sleep(KEY_GAP_S)

    def real_key(self, key) -> None:
        """A real key press (goes to whichever window has the keyboard on the INPUT desktop)."""
        self.u.keybd_event(key[0], key[1], 0, None)
        self.u.keybd_event(key[0], key[1], self.KEYEVENTF_KEYUP, None)
        time.sleep(KEY_GAP_S)

    @staticmethod
    def dialog_hwnd(env) -> int:
        """The stand-in dialog's window handle."""
        return node_named(env, DIALOG).h.CurrentNativeWindowHandle

    @staticmethod
    def focus_combo(env) -> None:
        """Ask UI Automation to give the combo keyboard focus."""
        node_named(env, ap.COMBO).h.SetFocus()
        time.sleep(SETTLE_S)

    def open_popup(self, env):
        """Expand the combo; return (popup window handle, the target item's element)."""
        combo = node_named(env, ap.COMBO)
        combo.h.GetCurrentPattern(ap.UIA_EXPAND_COLLAPSE).QueryInterface(
            self.uiac.IUIAutomationExpandCollapsePattern).Expand()
        time.sleep(SETTLE_S)
        dialog = self.dialog_hwnd(env)
        nodes = env.reader.nodes(env.pid)
        item = next((n for n in nodes
                     if n.name == TARGET and not ap.id_matches(n.ident, ap.COMBO)), None)
        if item is None:
            raise LookupError(f"no element named {TARGET!r} after expanding the combo")
        # The popup list is a top-level window of its own: the one that is not the dialog
        handles = [n.h.CurrentNativeWindowHandle or 0 for n in nodes]
        popup = next((h for h in handles
                      if h and h != dialog and self.u.GetAncestor(h, self.GA_ROOT) == h), 0)
        if not popup:
            raise LookupError("no popup window besides the dialog")
        return popup, item.h

    def posted_down_only(self, env):              # control: no focus call at all
        """Route: post Down to the dialog with no focus call."""
        self.post_key(self.dialog_hwnd(env), self.DOWN)

    def focus_posted_down(self, env):
        """Route: focus the combo through UI Automation, then post Down to the dialog."""
        self.focus_combo(env)
        self.post_key(self.dialog_hwnd(env), self.DOWN)

    def popup_keys_to_popup(self, env):
        """Route: open the list, then post Down and Return to the list's own window."""
        hwnd, _ = self.open_popup(env)
        self.post_key(hwnd, self.DOWN)
        self.post_key(hwnd, self.RETURN)

    def popup_keys_to_dialog(self, env):
        """Route: open the list, then post Down and Return to the dialog's window."""
        self.open_popup(env)
        self.post_key(self.dialog_hwnd(env), self.DOWN)
        self.post_key(self.dialog_hwnd(env), self.RETURN)

    def popup_posted_click(self, env):
        """Route: open the list, then post a mouse click at the item's position in the list."""
        hwnd, item = self.open_popup(env)
        rect = item.CurrentBoundingRectangle       # screen coordinates
        point = self.wt.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        if not self.u.ScreenToClient(hwnd, self.ctypes.byref(point)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        where = (point.y << 16) | (point.x & 0xFFFF)
        for msg, keys in ((self.WM_MOUSEMOVE, 0), (self.WM_LBUTTONDOWN, self.MK_LBUTTON),
                          (self.WM_LBUTTONUP, 0)):
            self.u.PostMessageW(hwnd, msg, keys, where)
            time.sleep(KEY_GAP_S)

    def popup_legacy_default(self, env):
        """Route: open the list, then run the item's LegacyIAccessible default action."""
        _, item = self.open_popup(env)
        item.GetCurrentPattern(self.LEGACY_PATTERN).QueryInterface(
            self.uiac.IUIAutomationLegacyIAccessiblePattern).DoDefaultAction()

    def focus_real_down(self, env):
        """Route: bring the dialog to the front, focus the combo, send one real Down key."""
        # Real keys go to the foreground window, so the row records whether the dialog could
        # be made that; where it could not, the route was not judged (QA CR-4).
        env.row["foreground_ok"] = bool(self.u.SetForegroundWindow(self.dialog_hwnd(env)))
        self.focus_combo(env)
        self.real_key(self.DOWN)

    def routes(self) -> list:
        """The Windows routes as (name, function) pairs, in the order they are tried."""
        return [("posted Down, no focus call (control)", self.posted_down_only),
                ("UIA SetFocus + posted Down", self.focus_posted_down),
                ("UIA Expand + posted Down, Return to the popup window", self.popup_keys_to_popup),
                ("UIA Expand + posted Down, Return to the dialog window",
                 self.popup_keys_to_dialog),
                ("UIA Expand + posted mouse click on the item", self.popup_posted_click),
                ("UIA Expand + LegacyIAccessible default action on the item",
                 self.popup_legacy_default),
                ("UIA SetFocus + real key Down (needs the input desktop)", self.focus_real_down)]


class LinuxRoutes:
    """Linux: AT-SPI for focus and finding; XTEST (xdotool) for input, inside the Xvfb session."""

    def __init__(self):
        """Load pyatspi (for the coordinate type AT-SPI positions are asked in)."""
        import pyatspi  # noqa: PLC0415 - Linux-only system package
        self.desktop_coords = pyatspi.DESKTOP_COORDS

    @staticmethod
    def xdo(*args: str) -> str:
        """Run xdotool with the given arguments and return its output."""
        return subprocess.run(["xdotool", *args], check=True, timeout=10, capture_output=True,
                              text=True).stdout

    def key(self, *names: str) -> None:
        """Press each named key in turn (real XTEST input)."""
        for name in names:
            self.xdo("key", name)
            time.sleep(KEY_GAP_S)

    def click(self, node) -> None:
        """A real (XTEST) click at the centre of where AT-SPI says the element is."""
        e = node.h.queryComponent().getExtents(self.desktop_coords)
        self.xdo("mousemove", str(e.x + e.width // 2), str(e.y + e.height // 2))
        self.xdo("click", "1")
        time.sleep(SETTLE_S)

    @staticmethod
    def grab_focus(env) -> None:
        """Ask AT-SPI to give the combo keyboard focus; raise when it says no."""
        if not node_named(env, ap.COMBO).h.queryComponent().grabFocus():
            raise RuntimeError("grabFocus returned false")
        time.sleep(SETTLE_S)

    def x_focus(self, env) -> None:
        """Give the stand-in's window X keyboard focus with xdotool."""
        # No window manager runs in the session, so nothing gives the window X keyboard focus
        wid = self.xdo("search", "--pid", str(env.pid), "--name", "QtStandin").split()[0]
        self.xdo("windowfocus", wid)
        time.sleep(SETTLE_S)

    @staticmethod
    def popup_item(env):
        """The target item in the combo's open list."""
        item = next((n for n in env.reader.nodes(env.pid) if n.name == TARGET
                     and n.h.parent is not None
                     and ap.id_matches(n.h.parent.get_accessible_id(), "QComboBoxListView")), None)
        if item is None:
            raise LookupError(f"no popup list item {TARGET!r}")
        return item

    @staticmethod
    def show_menu(env) -> None:
        """Open the combo's list with its AT-SPI action."""
        action = node_named(env, ap.COMBO).h.queryAction()
        names = [action.getName(i) for i in range(action.nActions)]
        action.doAction(names.index("ShowMenu") if "ShowMenu" in names else 0)
        time.sleep(SETTLE_S)

    def down_only(self, _env):                    # control: no focus call at all
        """Route: one real Down key with no focus call."""
        self.key("Down")

    def grab_down(self, env):
        """Route: AT-SPI focus on the combo, then a real Down key."""
        self.grab_focus(env)
        self.key("Down")

    def xfocus_grab_down(self, env):
        """Route: X focus on the window, AT-SPI focus on the combo, then a real Down key."""
        self.x_focus(env)
        self.grab_focus(env)
        self.key("Down")

    def click_down(self, env):                    # a click on the combo opens its list
        """Route: a real click on the combo, then real Down and Return keys."""
        self.click(node_named(env, ap.COMBO))
        self.key("Down", "Return")

    def click_click(self, env):
        """Route: a real click on the combo, then a real click on the item."""
        self.click(node_named(env, ap.COMBO))
        self.click(self.popup_item(env))

    def menu_keys(self, env):
        """Route: X focus, open the list by AT-SPI action, then real Down and Return keys."""
        self.x_focus(env)
        self.show_menu(env)
        self.key("Down", "Return")

    def menu_click(self, env):
        """Route: open the list by AT-SPI action, then a real click on the item."""
        self.show_menu(env)
        self.click(self.popup_item(env))

    def routes(self) -> list:
        """The Linux routes as (name, function) pairs, in the order they are tried."""
        return [("XTEST Down, no focus call (control)", self.down_only),
                ("AT-SPI grabFocus + XTEST Down", self.grab_down),
                ("xdotool windowfocus + AT-SPI grabFocus + XTEST Down", self.xfocus_grab_down),
                ("click at the combo's AT-SPI position + XTEST Down, Return", self.click_down),
                ("click at the combo's AT-SPI position + click at the item's", self.click_click),
                ("xdotool windowfocus + AT-SPI ShowMenu action + XTEST Down, Return",
                 self.menu_keys),
                ("AT-SPI ShowMenu action + click at the item's AT-SPI position", self.menu_click)]


class MacRoutes:
    """macOS: the Accessibility API for focus; key events posted to the process or the system."""

    DOWN, RETURN, SPACE = 125, 36, 49             # virtual key codes (Carbon Events.h)

    def __init__(self):
        """Load the macOS Accessibility and Quartz event modules."""
        import ApplicationServices as ax  # noqa: PLC0415 - macOS-only packages (pyobjc)
        import Quartz  # noqa: PLC0415
        self.ax, self.quartz = ax, Quartz

    def post(self, env, *codes: int) -> None:
        """Key presses posted straight to the stand-in's process: no global input."""
        for code in codes:
            for is_down in (True, False):
                self.quartz.CGEventPostToPid(
                    env.pid, self.quartz.CGEventCreateKeyboardEvent(None, code, is_down))
            time.sleep(KEY_GAP_S)

    def system_keys(self, *codes: int) -> None:
        """Key presses through the system input stream (go to the frontmost application)."""
        for code in codes:
            for is_down in (True, False):
                self.quartz.CGEventPost(
                    self.quartz.kCGHIDEventTap,
                    self.quartz.CGEventCreateKeyboardEvent(None, code, is_down))
            time.sleep(KEY_GAP_S)

    def set_attr(self, element, name: str, value) -> None:
        """Set one accessibility attribute; raise on an AXError."""
        err = self.ax.AXUIElementSetAttributeValue(element, name, value)
        if err != 0:
            raise OSError(f"set {name} returned AXError {err}")

    def focus(self, env) -> None:
        """Ask the Accessibility API to focus the combo (AXFocused)."""
        self.set_attr(node_named(env, ap.COMBO).h, "AXFocused", True)
        time.sleep(SETTLE_S)

    def press(self, env) -> None:
        """Press the combo through the Accessibility API (AXPress), which opens its list."""
        err = self.ax.AXUIElementPerformAction(node_named(env, ap.COMBO).h, "AXPress")
        if err != 0:
            raise OSError(f"AXPress returned AXError {err}")
        time.sleep(SETTLE_S)

    def front(self, env) -> None:
        """Make the stand-in the frontmost application (AXFrontmost)."""
        self.set_attr(self.ax.AXUIElementCreateApplication(env.pid), "AXFrontmost", True)
        time.sleep(SETTLE_S)

    def routes(self) -> list:
        """The macOS routes as (name, function) pairs, in the order they are tried."""
        down, ret, space = self.DOWN, self.RETURN, self.SPACE
        return [("posted Down, no focus call (control)", lambda env: self.post(env, down)),
                ("AXFocused + posted Down", lambda env: (self.focus(env), self.post(env, down))),
                ("AXFocused + posted Down, Down, Return",
                 lambda env: (self.focus(env), self.post(env, down, down, ret))),
                ("AXFocused + posted Space, Down, Return",
                 lambda env: (self.focus(env), self.post(env, space, down, ret))),
                ("AXPress + posted Down, Return",
                 lambda env: (self.press(env), self.post(env, down, ret))),
                ("AXFrontmost + AXFocused + system Down, Down, Return (real input)",
                 lambda env: (self.front(env), self.focus(env),
                              self.system_keys(down, down, ret))),
                ("AXFrontmost + AXPress + system Down, Return (real input)",
                 lambda env: (self.front(env), self.press(env), self.system_keys(down, ret)))]


ROUTES = {"linux": LinuxRoutes, "windows": WindowsRoutes, "macos": MacRoutes}


def one_route(name: str, fn, run: SimpleNamespace) -> None:
    """Start a stand-in of its own for this route, run the route, stop the stand-in.
    run carries reader, python, out and the result dict r."""
    out, r = run.out, run.r
    for stale in ("ready.json", "values.json", "state.json"):
        (out / stale).unlink(missing_ok=True)
    proc = ap.start_standin(run.python, out)
    if proc is None:
        r["routes"].append({"route": name, "took": False,
                            "error": f"stand-in wrote no ready.json in {ap.READY_WAIT_S} s"})
        return
    try:
        ready = json.loads((out / "ready.json").read_text())
        r["qt_version"] = ready.get("qt")
        env = SimpleNamespace(reader=run.reader, pid=ready["pid"], out=out, row={})
        if ap.find_by_id(ap.first_walk(run.reader, env.pid, r["notes"]), ap.COMBO) is None:
            r["routes"].append({"route": name, "took": False,
                                "error": "the combo is not in the accessibility tree"})
            return
        run_route(name, fn, env, r["routes"])
    finally:                                      # the stand-in never outlives its route
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def main() -> int:
    """Run every route for this OS and write probe-qt-combo-<os>[-label].json; exit 0."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--standin-python", default=sys.executable)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    os_name = ap.os_name()
    r = {"os": os_name, "label": args.label, "qt_version": None, "target": TARGET, "routes": [],
         "notes": []}
    if os_name == "windows":
        import fred2_live  # noqa: PLC0415 - for desktop_name(); Windows-only ctypes inside
        r["desktop"] = ap.attempt(r["notes"], "desktop name", fred2_live.desktop_name)
    reader = ap.attempt(r["notes"], "reader", ap.READERS[os_name])
    routes = (ap.attempt(r["notes"], "routes", lambda: ROUTES[os_name]().routes()) or []
              ) if reader is not None else []
    # WHY a fresh stand-in per route: a route can leave something behind that helps the next
    # one (a click gives the window keyboard focus for good), and then the later route's
    # "took" would not be its own. The first local run had exactly that ordering.
    run = SimpleNamespace(reader=reader, python=args.standin_python, out=out, r=r)
    name = f"probe-qt-combo-{os_name}" + (f"-{args.label}" if args.label else "")
    try:
        for route_name, fn in routes:
            ap.attempt(r["notes"], route_name, lambda f=fn, n=route_name: one_route(n, f, run))
    finally:                                      # rows already measured are never lost
        r["routes_that_took"] = [row["route"] for row in r["routes"] if row["took"]]
        (out / f"{name}.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
