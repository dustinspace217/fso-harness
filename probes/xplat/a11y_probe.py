"""Probe 2 (cross-platform Plan A, Task 6): can the OS accessibility layer drive a Qt 6 dialog?

The design adds an editor-driving method that works through each OS's accessibility layer (UI
Automation on Windows, the Accessibility API on macOS, AT-SPI on Linux) instead of pixels. qtFRED
is Qt 6; qt_standin.py is a small Qt dialog with the widget kinds an editor task touches. This
probe starts it and, through the accessibility layer only:

    object_name_exposed   finds the line edit by its Qt object name (and records which property
                          carries it: AutomationId / AXIdentifier / accessible-id, and its value)
    set_text_ok           sets the line edit's text and reads it back
    combo_select_ok       selects the second combo item and sees the combo show it
    tree_walk_ok          finds both tree items ("Event 1" and its child "Event 1a")
    press_ok              presses OK (the stand-in then writes values.json and closes)
    values_after_ok       the stand-in's values.json: what Qt itself saw
    needs_permission      macOS only: the process lacks Accessibility permission (Task 8 reads
                          this field rather than measuring it again)

Every failed step keeps its error text in "notes"; one failure does not stop the later steps.
The probe never fails on what it finds (exit 0).

    python a11y_probe.py --out DIR [--standin-python PATH]

Linux: run inside probes/xplat/linux_a11y_session.sh, which provides Xvfb and an AT-SPI bus.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

STANDIN = Path(__file__).resolve().with_name("qt_standin.py")
EDIT, COMBO, TREE, OK = "shipNameEdit", "shipClassCombo", "eventTree", "okButton"
NEW_NAME = "GTD Probe"
NEW_CLASS = "GTF Hercules"            # qt_standin.SHIP_CLASSES[1]; the combo starts on [0]
TREE_NAMES = ("Event 1", "Event 1a")
READY_WAIT_S = 30
VALUES_WAIT_S = 10
SETTLE_S = 0.5                        # after an action, before reading its effect back
POLL_S = 0.25
MAX_DEPTH = 12                        # accessibility trees are walked to this depth at most
MAX_NODES = 2000


def os_name() -> str:
    """Return "linux", "windows" or "macos" for the result file name."""
    return {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(platform.system(),
                                                                          platform.system())


def id_matches(identifier, object_name: str) -> bool:
    """True when an accessibility id names object_name: the whole id, or its last dotted part
    (Qt's AT-SPI ids are objectName paths, e.g. QApplication.standinDialog.shipNameEdit)."""
    return bool(identifier) and (identifier == object_name
                                 or str(identifier).endswith("." + object_name))


def find_by_id(nodes: list, object_name: str):
    """Return the first node whose id names object_name, or None."""
    return next((n for n in nodes if id_matches(n.ident, object_name)), None)


def wait_for(predicate, seconds: float) -> bool:
    """Poll predicate every POLL_S until it is true or seconds pass; return its last answer."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:        # bounded by the deadline
        if predicate():
            return True
        time.sleep(POLL_S)
    return bool(predicate())


def attempt(notes: list, label: str, fn):
    """Run fn(); return its value, or record "label: error" in notes and return None."""
    try:
        return fn()
    except Exception as e:     # a measurement: any failure is recorded, never raised
        notes.append(f"{label}: {type(e).__name__}: {e}")
        return None


def new_result() -> dict:
    """The result row with every field at its failed value."""
    return {"os": os_name(), "qt_version": None, "object_name_exposed": False,
            "object_name_property": None,
            "object_name_value": None, "set_text_ok": False, "combo_select_ok": False,
            "select_method": None, "tree_walk_ok": False, "press_ok": False,
            "values_after_ok": None, "qt_saw_name": False, "qt_saw_class": False,
            "needs_permission": None, "notes": []}


def judge_values(values, r: dict) -> None:
    """Compare what Qt itself recorded (values.json) with what the probe set."""
    r["values_after_ok"] = values
    if isinstance(values, dict):
        r["qt_saw_name"] = values.get("ship_name") == NEW_NAME
        r["qt_saw_class"] = values.get("ship_class") == NEW_CLASS


def first_walk(reader, pid: int, notes: list) -> list:
    """Walk the stand-in's tree until the line edit is in it, at most READY_WAIT_S: a toolkit
    can show its window before its accessibility tree is filled in."""
    deadline = time.monotonic() + READY_WAIT_S
    nodes = []
    while time.monotonic() < deadline:        # bounded by the deadline
        nodes = attempt(notes, "walk", lambda: reader.nodes(pid)) or []
        if find_by_id(nodes, EDIT) is not None:
            break
        time.sleep(POLL_S)
    return nodes


def select_combo(reader, pid: int, combo, r: dict) -> None:
    """Try each of the reader's combo-selection methods in turn until the combo shows NEW_CLASS;
    record what each returned and which one worked (select_method), so a false is a statement
    about every method tried, not about one."""
    for name, method in reader.select_methods:
        returned = attempt(r["notes"], f"select ({name})",
                           lambda m=method: m(pid, combo, NEW_CLASS))
        time.sleep(SETTLE_S)
        shown = attempt(r["notes"], "combo text", lambda: reader.combo_text(pid))
        r["notes"].append(f"select ({name}) returned {returned!r}; combo shows {shown!r}")
        if shown == NEW_CLASS:
            r["combo_select_ok"], r["select_method"] = True, name
            return


def drive(reader, pid: int, r: dict) -> None:
    """The same steps on every OS, through reader (one of the read_* namespaces below)."""
    notes = r["notes"]
    nodes = first_walk(reader, pid, notes)
    notes.append(f"{len(nodes)} accessible elements under the stand-in")
    edit, combo = find_by_id(nodes, EDIT), find_by_id(nodes, COMBO)
    tree, ok = find_by_id(nodes, TREE), find_by_id(nodes, OK)
    notes.append("ids found: " + ", ".join(
        f"{k}={'yes' if v else 'no'}" for k, v in
        ((EDIT, edit), (COMBO, combo), (TREE, tree), (OK, ok))))
    if edit is not None:
        r["object_name_exposed"] = True
        r["object_name_property"], r["object_name_value"] = reader.id_property, edit.ident
        attempt(notes, "set text", lambda: reader.set_text(edit, NEW_NAME))
        time.sleep(SETTLE_S)
        r["set_text_ok"] = attempt(notes, "read text", lambda: reader.read_text(edit)) == NEW_NAME
    if combo is not None:
        select_combo(reader, pid, combo, r)
    names = {n.name for n in (attempt(notes, "rewalk", lambda: reader.nodes(pid)) or [])}
    r["tree_walk_ok"] = all(t in names for t in TREE_NAMES)
    if ok is not None:
        r["press_ok"] = attempt(notes, "press", lambda: reader.press(ok)) is True


def start_standin(python: str, out: Path):
    """Start qt_standin.py with python; return the Popen once its ready.json exists, or None."""
    env = dict(os.environ, QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1")   # read by Qt on Linux only
    with open(out / "standin.log", "w") as log:     # the child keeps its own copy of the handle
        proc = subprocess.Popen([python, str(STANDIN), str(out)], env=env, stdout=log,
                                stderr=subprocess.STDOUT)
    if not wait_for((out / "ready.json").exists, READY_WAIT_S):
        proc.kill()
        proc.wait()
        return None
    return proc


def measure(reader, out: Path, r: dict) -> None:
    """With the stand-in up: permission (macOS), the drive sequence, and Qt's own values."""
    if os_name() == "macos":
        trusted = attempt(r["notes"], "AXIsProcessTrusted", reader.trusted)
        r["needs_permission"] = None if trusted is None else not trusted
    ready = attempt(r["notes"], "ready.json", lambda: json.loads((out / "ready.json").read_text()))
    if not isinstance(ready, dict) or "pid" not in ready:
        return
    r["qt_version"] = ready.get("qt")
    drive(reader, ready["pid"], r)
    if wait_for((out / "values.json").exists, VALUES_WAIT_S):
        judge_values(attempt(r["notes"], "values.json",
                             lambda: json.loads((out / "values.json").read_text())), r)


def main() -> int:
    """Run the probe on this OS and write probe-qt-a11y-<os>.json; always exit 0."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--standin-python", default=sys.executable)
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("ready.json", "values.json"):
        (out / stale).unlink(missing_ok=True)
    r = new_result()
    reader = attempt(r["notes"], "reader", READERS[os_name()])
    proc = start_standin(args.standin_python, out) if reader is not None else None
    if reader is not None and proc is None:
        r["notes"].append(f"stand-in wrote no ready.json in {READY_WAIT_S} s")
    if proc is not None:
        try:
            measure(reader, out, r)
        finally:                                  # the stand-in never outlives the probe
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    r["display"] = os.environ.get("DISPLAY")      # Linux: shows which X server was measured
    (out / f"probe-qt-a11y-{os_name()}.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))
    return 0


# ---- Linux: AT-SPI through pyatspi (the system package; the venv sees it) ----

def read_linux() -> SimpleNamespace:
    """AT-SPI reader. Qt 6.12 puts the objectName path in each element's accessible-id
    (QApplication.standinDialog.shipNameEdit, seen locally 2026-10-25)."""
    import pyatspi  # noqa: PLC0415 - Linux-only system package

    def app_for(pid):
        desktop = pyatspi.Registry.getDesktop(0)
        app = next((a for a in desktop if a is not None and a.get_process_id() == pid), None)
        if app is None:
            raise LookupError(f"pid {pid} is not on the AT-SPI bus")
        return app

    def nodes(pid):
        found, stack = [], [(app_for(pid), 0)]
        while stack and len(found) < MAX_NODES:          # bounded by MAX_NODES and MAX_DEPTH
            acc, depth = stack.pop()
            found.append(SimpleNamespace(h=acc, name=acc.name, ident=acc.get_accessible_id()))
            if depth < MAX_DEPTH:
                stack.extend((c, depth + 1) for c in acc if c is not None)
        return found

    def press(node):
        action = node.h.queryAction()
        names = [action.getName(i) for i in range(action.nActions)]
        return bool(action.doAction(names.index("Press")))

    def list_item(pid, item):
        items = [n for n in nodes(pid) if n.name == item and n.h.parent is not None
                 and id_matches(n.h.parent.get_accessible_id(), "QComboBoxListView")]
        if not items:
            raise LookupError(f"no combo list item {item!r}")
        return items[0].h

    def select_child(pid, _combo, item):
        acc = list_item(pid, item)
        return acc.parent.querySelection().selectChild(acc.getIndexInParent())

    def toggle(pid, _combo, item):
        action = list_item(pid, item).queryAction()
        names = [action.getName(i) for i in range(action.nActions)]
        return action.doAction(names.index("Toggle"))

    def combo_text(pid):
        return find_by_id(nodes(pid), COMBO).h.name

    return SimpleNamespace(
        id_property="accessible-id", nodes=nodes, press=press, combo_text=combo_text,
        # Two routes; a local run on 2026-10-25 also tried both after the combo's "Show Menu"
        # action, with the same result (see the results note).
        select_methods=[("list Selection.selectChild", select_child),
                        ("item Toggle action", toggle)],
        set_text=lambda n, s: n.h.queryEditableText().setTextContents(s),
        read_text=lambda n: n.h.queryText().getText(0, -1))


# ---- Windows: UI Automation through comtypes ----

UIA_INVOKE, UIA_SELECTION_ITEM, UIA_VALUE, UIA_EXPAND_COLLAPSE = 10000, 10010, 10002, 10005
UIA_PROCESS_ID = 30002
TREESCOPE_CHILDREN, TREESCOPE_DESCENDANTS = 2, 4               # UIAutomationClient.h values


def read_windows() -> SimpleNamespace:
    """UI Automation reader; the object name is looked for in each element's AutomationId."""
    import comtypes.client  # noqa: PLC0415 - Windows-only package
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as uiac  # noqa: PLC0415 - generated just above
    uia = comtypes.client.CreateObject(uiac.CUIAutomation, interface=uiac.IUIAutomation)

    def pattern(node, pid_, iface):
        return node.h.GetCurrentPattern(pid_).QueryInterface(iface)

    def nodes(pid):
        # Every top-level window of the process: an open combo popup may be a window of its
        # own, which FindFirst could return instead of the dialog (QA CR-1).
        cond = uia.CreatePropertyCondition(UIA_PROCESS_ID, pid)
        windows = uia_list(uia.GetRootElement().FindAll(TREESCOPE_CHILDREN, cond))
        if not windows:
            raise LookupError(f"no top-level UI Automation element for pid {pid}")
        found = []
        for window in windows:
            found += [window, *uia_list(window.FindAll(TREESCOPE_DESCENDANTS,
                                                       uia.CreateTrueCondition()))]
        return [SimpleNamespace(h=e, name=e.CurrentName, ident=e.CurrentAutomationId)
                for e in found[:MAX_NODES]]

    def expanded_item(pid, combo, item):
        pattern(combo, UIA_EXPAND_COLLAPSE, uiac.IUIAutomationExpandCollapsePattern).Expand()
        time.sleep(SETTLE_S)
        # Not the combo itself: compared by id, since nodes() makes new wrappers each call.
        match = next((n for n in nodes(pid) if n.name == item
                      and not id_matches(n.ident, COMBO)), None)
        if match is None:
            raise LookupError(f"no element named {item!r} after expanding the combo")
        return match

    def collapse(combo):
        attempt([], "collapse", lambda: pattern(
            combo, UIA_EXPAND_COLLAPSE, uiac.IUIAutomationExpandCollapsePattern).Collapse())

    def select(pid, combo, item):
        pattern(expanded_item(pid, combo, item), UIA_SELECTION_ITEM,
                uiac.IUIAutomationSelectionItemPattern).Select()
        collapse(combo)
        return True

    def invoke(pid, combo, item):
        pattern(expanded_item(pid, combo, item), UIA_INVOKE,
                uiac.IUIAutomationInvokePattern).Invoke()
        collapse(combo)
        return True

    def combo_text(pid):
        combo = find_by_id(nodes(pid), COMBO)
        value = attempt([], "value", lambda: pattern(combo, UIA_VALUE,
                                                     uiac.IUIAutomationValuePattern).CurrentValue)
        return value if value else combo.name

    def press(node):
        pattern(node, UIA_INVOKE, uiac.IUIAutomationInvokePattern).Invoke()
        return True

    return SimpleNamespace(
        id_property="AutomationId", nodes=nodes, press=press, combo_text=combo_text,
        select_methods=[("expand + SelectionItem.Select", select),
                        ("expand + Invoke on the item", invoke)],
        set_text=lambda n, s: pattern(n, UIA_VALUE, uiac.IUIAutomationValuePattern).SetValue(s),
        read_text=lambda n: pattern(n, UIA_VALUE, uiac.IUIAutomationValuePattern).CurrentValue)


def uia_list(array) -> list:
    """Return the elements of an IUIAutomationElementArray, at most MAX_NODES of them."""
    return [array.GetElement(i) for i in range(min(array.Length, MAX_NODES))]


# ---- macOS: the Accessibility API through pyobjc ----

def read_macos() -> SimpleNamespace:
    """AX reader; the object name is looked for in each element's AXIdentifier."""
    import ApplicationServices as ax  # noqa: PLC0415 - macOS-only package (pyobjc)

    def attr(el, name):
        err, value = ax.AXUIElementCopyAttributeValue(el, name, None)
        return value if err == 0 else None

    def label(el):
        return attr(el, "AXTitle") or attr(el, "AXValue") or attr(el, "AXDescription")

    def nodes(pid):
        found, stack = [], [(ax.AXUIElementCreateApplication(pid), 0)]
        while stack and len(found) < MAX_NODES:          # bounded by MAX_NODES and MAX_DEPTH
            el, depth = stack.pop()
            found.append(SimpleNamespace(h=el, name=label(el), ident=attr(el, "AXIdentifier")))
            if depth < MAX_DEPTH:
                stack.extend((c, depth + 1) for c in (attr(el, "AXChildren") or []))
        return found

    def act(node, action):
        err = ax.AXUIElementPerformAction(node.h, action)
        if err != 0:
            raise OSError(f"{action} returned AXError {err}")
        return True

    def press_item(pid, combo, item):
        act(combo, "AXPress")                            # expected to open the popup list
        time.sleep(SETTLE_S)
        match = next((n for n in nodes(pid) if n.name == item
                      and not id_matches(n.ident, COMBO)), None)
        if match is None:
            raise LookupError(f"no element named {item!r} after opening the combo")
        return act(match, "AXPress")

    def set_text(node, text):
        err = ax.AXUIElementSetAttributeValue(node.h, "AXValue", text)
        if err != 0:
            raise OSError(f"set AXValue returned AXError {err}")
        return True

    return SimpleNamespace(
        id_property="AXIdentifier", nodes=nodes, set_text=set_text,
        select_methods=[("open + AXPress on the item", press_item),
                        ("AXValue on the combo", lambda _pid, c, item: set_text(c, item))],
        press=lambda n: act(n, "AXPress"), read_text=lambda n: attr(n.h, "AXValue"),
        combo_text=lambda pid: label(find_by_id(nodes(pid), COMBO).h),
        trusted=lambda: bool(ax.AXIsProcessTrusted()))


READERS = {"linux": read_linux, "windows": read_windows, "macos": read_macos}

if __name__ == "__main__":
    sys.exit(main())
