"""Probe 3 (cross-platform Plan A, Task 7): what FRED2 shows to UI Automation.

FRED2 is the Windows-only MFC mission editor. Its dialogs are defined in fred2/fred.rc with
numeric control IDs named in fred2/resource.h (IDD_SHIP_EDITOR = 176, IDC_SHIP_NAME = 1126, ...).
For classic Win32 controls, UI Automation is expected to report the control ID as the
element's AutomationId; if it does, a task map can name FRED2 controls by resource ID.

This probe starts FRED2 with no game data, takes whatever window appears (main window or an
error dialog), and records:

    window_found                     a top-level element of the FRED2 process appeared
    dialog_controls                  [{AutomationId, ControlType, Name}] of that window's tree
    automation_id_is_resource_id     at least one numeric AutomationId is an IDC_ control ID in
                                     resource.h and none is unexplained (Windows' own IDs such as
                                     IDC_STATIC and IDOK do not count against it; None when no
                                     id was numeric)
    id_buckets                       the numeric ids, sorted: fred2 / windows / combo_child / other
    notes                            each id's bucket and name, and any error text

With no data the window is likely a message box, whose button IDs (IDOK = 1, ...) are Windows'
own; such a result says only that IDs come through. Step 2 of the plan (the owner's Windows 10
with retail data, the Ship Editor) is the real test. Exit 0 whatever is found.

    python fred2_uia.py --out DIR --fred2 PATH --resource-h PATH
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WINDOW_WAIT_S = 30
POLL_S = 0.5
MAX_NODES = 500
UIA_PROCESS_ID = 30002
TREESCOPE_CHILDREN, TREESCOPE_DESCENDANTS = 2, 4             # UIAutomationClient.h values
DEFINE = re.compile(r"^#define\s+(\w+)\s+(-?\d+)\s*$")


def resource_ids(text: str) -> dict:
    """Map each numeric value defined in a resource.h to the names that define it."""
    ids: dict = {}
    for line in text.splitlines():
        m = DEFINE.match(line.strip())
        if m:
            ids.setdefault(int(m.group(2)), []).append(m.group(1))
    return ids


def numeric_id(automation_id):
    """Return automation_id as an int when it is a (possibly negative) number, else None."""
    text = str(automation_id or "")
    digits = text.removeprefix("-")
    return int(text) if digits.isascii() and digits.isdecimal() else None


# IDs a FRED2 dialog carries that resource.h does not define (QA CR-2, TA-1): IDC_STATIC (-1,
# MFC's afxres.h; 392 uses in fred.rc) and the standard dialog buttons IDOK..IDHELP (1-9,
# winuser.h). 65535 is -1 read as an unsigned 16-bit ID.
WINDOWS_IDS = {-1, 65535, *range(1, 10)}
# The edit and list a dropdown combo box makes for itself; their numbers are recalled, not
# checked, so they get a bucket of their own rather than counting as matched.
COMBO_CHILD_IDS = {1000, 1001}


def compare_ids(controls: list, ids: dict):
    """Sort the numeric AutomationIds of controls into buckets: "fred2" (an IDC_ control ID in
    resource.h), "windows" (WINDOWS_IDS), "combo_child" (COMBO_CHILD_IDS), "other". A dialog or
    menu ID with the same number is not a match: the first CI run matched an SDL message box's
    button 100 to IDD_ABOUTBOX. Returns (verdict, buckets, notes): True when at least one FRED2
    ID was found and nothing is "other", False otherwise, None when no id is numeric."""
    buckets = {"fred2": [], "windows": [], "combo_child": [], "other": []}
    notes = []
    for c in controls:
        num = numeric_id(c["AutomationId"])
        if num is None:
            continue
        names = ids.get(num, [])
        controls_named = [n for n in names if n.startswith("IDC_")]
        if controls_named:
            bucket = "fred2"
        elif num in WINDOWS_IDS:
            bucket = "windows"
        else:
            bucket = "combo_child" if num in COMBO_CHILD_IDS else "other"
        buckets[bucket].append(num)
        notes.append(f"AutomationId {num} ({c['Name']!r}): {bucket}"
                     + (f" {', '.join(controls_named)}" if controls_named else "")
                     + (f" (resource.h: {', '.join(names)})" if names and not controls_named
                        else ""))
    if not any(buckets.values()):
        return None, buckets, ["no numeric AutomationId to compare"]
    return bool(buckets["fred2"]) and not buckets["other"], buckets, notes


def dump_window(pid: int):
    """Wait for a top-level UI Automation element of pid; return its subtree as dicts (or None
    when no window appeared within WINDOW_WAIT_S)."""
    import comtypes.client  # noqa: PLC0415 - Windows-only package
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as uiac  # noqa: PLC0415 - generated just above
    uia = comtypes.client.CreateObject(uiac.CUIAutomation, interface=uiac.IUIAutomation)
    cond = uia.CreatePropertyCondition(UIA_PROCESS_ID, pid)
    deadline = time.monotonic() + WINDOW_WAIT_S
    window = None
    while window is None and time.monotonic() < deadline:     # bounded by the deadline
        window = uia.GetRootElement().FindFirst(TREESCOPE_CHILDREN, cond) or None
        if window is None:
            time.sleep(POLL_S)
    if window is None:
        return None
    found = window.FindAll(TREESCOPE_DESCENDANTS, uia.CreateTrueCondition())
    elements = [window] + [found.GetElement(i) for i in range(min(found.Length, MAX_NODES))]
    return [{"AutomationId": e.CurrentAutomationId, "ControlType": e.CurrentControlType,
             "Name": e.CurrentName} for e in elements]


def main() -> int:
    """Start FRED2, dump its first window, write probe-fred2-uia.json; always exit 0."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--fred2", type=Path, required=True)
    ap.add_argument("--resource-h", type=Path, required=True)
    args = ap.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    game = out / "fred2-game"                  # an empty folder: no game data, by design
    game.mkdir(exist_ok=True)
    r = {"window_found": False, "dialog_controls": [], "automation_id_is_resource_id": None,
         "notes": []}
    # WHY ALSOFT_DRIVERS=null: the runner has no audio device, and FRED2 then stops at "OpenAL
    # 1.1 or newer is required" (first CI run). OpenAL Soft's null backend is a working device
    # with no output, so FRED2 goes on to whatever it shows with no game data.
    env = dict(os.environ, ALSOFT_DRIVERS="null")
    proc = subprocess.Popen([str(args.fred2.resolve())], cwd=game, env=env)
    try:
        controls = dump_window(proc.pid)
    except Exception as e:     # a measurement: any failure is recorded, never raised
        controls = None
        r["notes"].append(f"UI Automation: {type(e).__name__}: {e}")
    finally:
        proc.kill()
        proc.wait()
    if controls is not None:
        r["window_found"], r["dialog_controls"] = True, controls
        verdict, buckets, notes = compare_ids(controls,
                                              resource_ids(args.resource_h.read_text()))
        r["automation_id_is_resource_id"], r["id_buckets"] = verdict, buckets
        r["notes"].extend(notes)
    elif not r["notes"]:
        r["notes"].append(f"no top-level window from FRED2 in {WINDOW_WAIT_S} s")
    (out / "probe-fred2-uia.json").write_text(json.dumps(r, indent=1))
    print(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
