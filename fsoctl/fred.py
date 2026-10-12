"""qtFRED sessions: open a COPY of a mission, save it, read the camera, select by marquee.

Safety: qtFRED only ever opens a copy in the harness runtime folder, never the user's mission file
(retail missions are read out of their .vp archive), so Ctrl+S can only write the copy.

What the probes showed (docs/research/2026-10-12-probe-results.md section 10): qtFRED needs
-noninteractive (the launcher adds it) or a debug build stops on warning boxes; its window title is
the mission file name, so it is found by process id; its default window is small, so open()
resizes it; with no window manager a click on the status bar gives it keyboard focus.
"""
import argparse
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from . import launcher, paths, session, vp, xinput, xwin
from .cli import register
from .errors import EXIT_LAUNCH, EXIT_NOT_FOUND, EXIT_TIMEOUT, EXIT_USAGE, HarnessError

_SAVE_TIMEOUT = 15.0
_SIZE_SETTLE = 0.3   # a saved file counts as written when two size reads this far apart agree
# After a resize the 3D view keeps drawing at the old size for a moment (screenshot, 2026-10-12).
_LAYOUT_SETTLE = 3.0
_OCR_TIMEOUT = 30
_STATUS_LINE = "420x30+0+70"   # "No Selection" / selection summary, under the toolbars
_NUM = r"(-?\d+(?:\.\d+)?)"
_VIEW_POS = re.compile(r"\+Viewer pos:\s*" + r",\s*".join([_NUM] * 3))
_VIEW_ORIENT = re.compile(r"\+Viewer orient:\s*" + r",\s*".join([_NUM] * 9))


def _archives(root: Path) -> list[Path]:
    """Return the .vp archives directly in `root`, in the engine's order.

    The engine matches *.[vV][pP] and sorts with stricmp (cfilesystem.cpp cf_build_pack_list,
    cf_packfile_sort_func), so the order here is case-insensitive by name.
    """
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.suffix.lower() == ".vp" and p.is_file()]
    return sorted(found, key=lambda p: p.name.lower())


def find_mission(name: str, mods: list[str], fs2: Path,
                 skipped: list[dict] | None = None) -> tuple[bytes, str]:
    """Return (mission bytes, where it came from) for a mission name or absolute path.

    Search order is the engine's (cfilesystem.cpp: cf_add_mod_roots builds, for each -mod folder
    in order, the folder itself and then its archives; FS2PATH and its archives come last; file
    lookup takes the first match in that order). So a mod's archive beats a LATER mod's loose
    file, and within one folder a loose file beats the archives. Not covered: .vp files inside
    data subfolders and compressed .vpc archives, which the engine also reads.

    A damaged archive does not end the search: it is skipped, and when `skipped` is given each
    one is appended as {"archive", "error"}. The not-found error names the skipped archives.
    """
    if name.startswith("/"):
        p = Path(name)
        if not p.is_file():
            raise HarnessError(EXIT_NOT_FOUND, f"no mission file {name}", "check the path")
        return p.read_bytes(), str(p)
    bad: list[dict] = [] if skipped is None else skipped
    roots = [fs2 / m for m in mods] + [fs2]
    for root in roots:
        loose = root / "data" / "missions" / name
        if loose.is_file():
            return loose.read_bytes(), str(loose)
        for arc in _archives(root):
            try:
                return vp.read_file(arc, f"data/missions/{name}"), str(arc)
            except HarnessError as e:
                if e.code == EXIT_NOT_FOUND:
                    continue
                if e.code != EXIT_USAGE:   # e.g. EXIT_REFUSED: found, but too big to read
                    raise
                bad.append({"archive": str(arc), "error": e.message})
            except OSError as e:   # unreadable file: skip it like a damaged one
                bad.append({"archive": str(arc), "error": str(e)})
    also = f"; skipped damaged archives: {[b['archive'] for b in bad]}" if bad else ""
    raise HarnessError(EXIT_NOT_FOUND,
                       f"mission {name} not found in {[str(r) for r in roots]}{also}",
                       "give the file name as in data/missions, or an absolute path")


def parse_viewer(text: str) -> dict:
    """Parse the +Viewer pos/orient block of a mission: {"pos", "orient" (3x3), "fvec"}."""
    pos = _VIEW_POS.search(text)
    orient = _VIEW_ORIENT.search(text)
    if not pos or not orient:
        raise HarnessError(EXIT_NOT_FOUND, "no +Viewer block in the mission",
                           "save the mission first (fred_save)")
    m = [float(x) for x in orient.groups()]
    rows = [m[0:3], m[3:6], m[6:9]]
    return {"pos": [float(x) for x in pos.groups()], "orient": rows, "fvec": rows[2]}


def _window(sess: session.Session) -> str:
    """Return qtFRED's main window id (found by process id)."""
    return xinput.window_id(sess)


def _focus(sess: session.Session) -> None:
    """Click the status bar (bottom of the editor window) so keys reach qtFRED: there is no
    window manager to give it focus, and the status bar is a spot where a click changes nothing."""
    xinput.focus_neutral(sess, int(sess.width * 0.7), sess.height - 15)


def _stage(mission: str, mods: list[str], skipped: list[dict] | None = None) -> tuple[str, str]:
    """Copy the mission into a fresh fred-missions folder; return (copy path, its source).

    skipped, when given, collects the damaged archives the search passed over (find_mission).
    """
    data, source = find_mission(mission, mods, paths.fs2path(), skipped)
    stage = paths.runtime_root() / "fred-missions" / secrets.token_hex(4)
    stage.mkdir(parents=True)
    copy = stage / Path(mission).name
    copy.write_bytes(data)
    return str(copy), source


def stage_mission(mission: str, mods: list[str]) -> str:
    """Copy a mission (name in data/missions, inside a .vp, or absolute path) into the runtime
    folder and return the copy's absolute path: runtime_root()/fred-missions/<token>/<name>.

    WHY its own function: launch refuses a qtFRED mission outside fred-missions (C9), so every
    caller that opens a mission in qtFRED stages it here first; a fresh token per call means two
    sessions never edit the same copy.
    """
    return _stage(mission, mods)[0]


def open(mission: str, mods: list[str] | None = None, binary: str | None = None,
         width: int = 1280, height: int = 720) -> dict:
    """Copy a mission into the runtime folder and open the copy in qtFRED; return id + copy path.

    If anything fails after qtFRED was launched, the session is stopped and its id goes into the
    error's details, so a failed open never leaves an editor running unseen (QA SL-9, CR-5).
    """
    mods = mods or []
    exe = binary or os.environ.get("QTFRED_BIN")
    if not exe:
        raise HarnessError(EXIT_USAGE, "no qtFRED binary", "pass binary or export QTFRED_BIN")
    skipped: list[dict] = []
    copy, source = _stage(mission, mods, skipped)
    r = launcher.launch(launcher.LaunchRequest(binary=exe, mission=copy, mods=mods,
                                               kind="qtfred", width=width, height=height))
    try:
        sess = session.load(r["id"])
        wid = _window(sess)
        # Fixed geometry so screenshots, crops and click positions mean the same thing every time.
        xinput._x(sess, "xdotool", "windowmove", wid, "0", "0")
        xinput._x(sess, "xdotool", "windowsize", wid, str(width), str(height))
        time.sleep(_LAYOUT_SETTLE)
    except BaseException as e:   # also SystemExit from fsoctl's SIGTERM handler (C8)
        launcher.stop(r["id"])
        if isinstance(e, HarnessError):
            e.details.setdefault("id", r["id"])
        raise
    return {"id": r["id"], "mission_copy": copy, "source": source, "skipped_archives": skipped}


def _visible_titles(sess: session.Session) -> list[str]:
    """Return the titles of qtFRED's visible windows (editor and any dialogs)."""
    return [title for _, title in xwin.windows(sess.display or "", sess.engine_pid or 0)]


def _stable_size(path: Path, deadline: float) -> int | None:
    """Return the file's size once two reads _SIZE_SETTLE apart agree; None at the deadline."""
    while time.monotonic() < deadline:
        first = path.stat().st_size
        time.sleep(_SIZE_SETTLE)
        size = path.stat().st_size
        if size == first:
            return size
    return None


def save(sid: str) -> dict:
    """Press Ctrl+S; return {"path", "saved", "dialog", "size"} (dialog = the error check opened).

    "saved" waits for the size to settle after the file changes: the mtime moves at qtFRED's
    first write, before the file is complete, and fred_camera parses it right after (QA SF-17).
    """
    sess = launcher.require_running(sid)
    copy = Path(sess.mission or "")
    before = copy.stat().st_mtime_ns if copy.exists() else 0
    _focus(sess)
    xinput._x(sess, "xdotool", "windowfocus", _window(sess))
    xinput._x(sess, "xdotool", "key", "ctrl+s")
    deadline = time.monotonic() + _SAVE_TIMEOUT
    while time.monotonic() < deadline:
        if copy.exists() and copy.stat().st_mtime_ns != before:
            size = _stable_size(copy, deadline)
            if size is None:
                raise HarnessError(EXIT_TIMEOUT,
                                   f"qtFRED was still writing {copy} after {_SAVE_TIMEOUT:.0f}s",
                                   "take a screenshot, then run fred_save again")
            return {"path": str(copy), "saved": True, "dialog": False, "size": size}
        if "Pre-save Error Check" in _visible_titles(sess):
            return {"path": str(copy), "saved": False, "dialog": True, "size": None}
        time.sleep(0.25)
    raise HarnessError(EXIT_TIMEOUT, f"qtFRED did not save in {_SAVE_TIMEOUT:.0f}s",
                       "take a screenshot; another dialog may be open")


def camera(sid: str) -> dict:
    """Save, then return the camera position/orientation qtFRED wrote into the copy."""
    r = save(sid)
    if not r["saved"]:
        raise HarnessError(EXIT_LAUNCH, "the pre-save error check opened instead of saving",
                           "run fred_check to read it")
    return parse_viewer(Path(r["path"]).read_bytes().decode("latin-1"))


def ocr(path: str) -> str:
    """Return tesseract's text for an image (block-of-text mode)."""
    r = subprocess.run(["tesseract", path, "-", "--psm", "6"], capture_output=True, text=True,
                       timeout=_OCR_TIMEOUT, check=False)
    if r.returncode != 0:
        raise HarnessError(EXIT_LAUNCH, f"tesseract failed: {r.stderr.strip()[:200]}",
                           "is tesseract installed?")
    return r.stdout.strip()


SELECTION_NOTE = (f"read by OCR from a fixed crop ({_STATUS_LINE}) of qtFRED's status line; "
                  "check the screenshot before acting on exact names")


def selection(sid: str) -> dict:
    """Return {"selection": status-line text (OCR), "screenshot", "note" (the OCR caveat)}."""
    shot = xinput.screenshot(sid, crop=_STATUS_LINE)
    return {"selection": ocr(shot["path"]), "screenshot": shot["path"], "note": SELECTION_NOTE}


_SELECT_POLLS = 10        # status-line reads per drag (every _SELECT_POLL_SEC, ~3 s)
_SELECT_POLL_SEC = 0.3


def select(sid: str, rect: tuple[int, int, int, int]) -> dict:
    """Marquee-select the window rectangle (x1, y1, x2, y2); return the status line text.

    Reads the status line before the drag, then polls it until the text changes. WHY: qtFRED
    repaints the status line some time after the drag; a single read 0.5 s later caught the old
    "No Selection" in 1 qtFRED tier run of 3 (2026-10-12). If nothing changes after one round,
    the same drag is sent once more (it selects the same rectangle, so repeating it is harmless).
    Returns selection(sid) plus "changed" (False when the text never moved, which also happens
    when the rectangle selects exactly what was selected before) and "drags" (1 or 2).
    """
    x1, y1, x2, y2 = rect
    before = selection(sid)["selection"]
    for drags in (1, 2):
        xinput.drag(sid, (x1, y1), (x2, y2))
        for _ in range(_SELECT_POLLS):
            time.sleep(_SELECT_POLL_SEC)
            now = selection(sid)
            if now["selection"] != before:
                return {**now, "changed": True, "drags": drags}
    return {**now, "changed": False, "drags": drags}


# ---- pre-save error check (Task 17) -----------------------------------------------------------
# Ctrl+S on a mission with problems opens qtFRED's "Pre-save Error Check" dialog instead of saving
# (probe results section 10). Its text is read by OCR from a 2x screenshot of the dialog alone:
# whole-screen OCR was noise, the dialog alone at 2x read cleanly.

_CHECK_TITLE = "Pre-save Error Check"
_ENTRY_HEAD = re.compile(r"^(Critical Error|Error|Warning|Potential [Ii]ssue)\b")
_SEVERITY = {"critical error": "critical", "error": "error", "warning": "warning"}
_COUNT = re.compile(r"(\d+)\s+error\(s\) found", re.IGNORECASE)
_BAD_NODE = re.compile(r"\(Bad node appears to be:\s*[\"\u201c\u201d]?(.*?)[\"\u201c\u201d]?\)")
_DIALOG_CLOSE_SEC = 5.0
_CHECK_TRIES = 5          # captures of the dialog before giving up on a consistent reading
_CHECK_RETRY_SEC = 0.4    # pause between captures, so Qt can finish painting
SAVED_CLEAN_NOTE = ("qtFRED's pre-save error check ran and found no errors, so it saved without "
                    "showing its dialog; it does not check potential issues on save (run Tools > "
                    "Error Checker, Shift+H, for those)")
CHECK_NOTE = ("read by OCR from qtFRED's pre-save error check; verify against the screenshot "
              "before acting on exact names")


def _is_footer(line: str) -> bool:
    """True for the dialog's explanation text under the entries."""
    return "Fix and Save" in line or "Complex errors" in line or "loadout poo" in line


def parse_checker(text: str) -> dict:
    """Parse OCR text of the dialog: {"count": int | None, "entries": [{severity, text, bad_node}]}.

    The legend line (all four severity names) and the footer are skipped; each entry starts with
    its severity word; "(Bad node appears to be: X)" is attached to the entry above it.
    """
    count, entries = None, []
    for raw in text.splitlines():
        line = raw.strip()
        m_count = _COUNT.search(line)
        if m_count:
            count = int(m_count.group(1))
            continue
        if not line or _is_footer(line) or ("Critical Error" in line and "Potential" in line):
            continue
        head = _ENTRY_HEAD.match(line)
        if head:
            word = head.group(1).lower()
            entries.append({"severity": _SEVERITY.get(word, "potential"), "text": line,
                            "bad_node": None})
            continue
        bad = _BAD_NODE.search(line)
        if bad and entries:
            entries[-1]["bad_node"] = bad.group(1)
    return {"count": count, "entries": entries}


def _dialog_id(sess: session.Session) -> str | None:
    """Return the pre-save error check window id, or None."""
    wins = xwin.windows(sess.display or "", sess.engine_pid or 0)
    return next((w for w, title in wins if title == _CHECK_TITLE), None)


def _close_dialog(sess: session.Session, did: str) -> None:
    """Close the dialog with Escape (its Cancel), bounded; raise if it stays open."""
    xinput._x(sess, "xdotool", "windowfocus", did)
    xinput._x(sess, "xdotool", "key", "Escape")
    deadline = time.monotonic() + _DIALOG_CLOSE_SEC
    while time.monotonic() < deadline:
        if _dialog_id(sess) is None:
            return
        time.sleep(0.25)
    raise HarnessError(EXIT_TIMEOUT, "the pre-save error check did not close on Escape",
                       "take a screenshot; close it with fred_select/click on Cancel")


def check(sid: str) -> dict:
    """Run qtFRED's error check by saving; return clean, count, entries, count_matches_entries,
    screenshot, raw text."""
    r = save(sid)
    if r["saved"]:
        # qtFRED runs the check inside save and opens the dialog only when it finds errors
        # (qtfred/src/ui/FredView.cpp:433-440), so a save that went through IS a clean check. The
        # old note ("saved without opening the error check") read as if no check had run.
        # Potential-issue checks are switched off for that save (FredView.cpp:427).
        return {"clean": True, "count": 0, "entries": [], "count_matches_entries": True,
                "screenshot": None, "raw_text": "", "note": SAVED_CLEAN_NOTE}
    sess = session.load(sid)
    did = _dialog_id(sess)
    if not did:
        raise HarnessError(EXIT_NOT_FOUND, "the error check closed before it could be read",
                           "run fred_check again")
    shots = session.run_dir(sess) / "shots"
    shots.mkdir(exist_ok=True)
    shot = xinput.new_shot_path(shots, "checker")
    # The dialog states its own entry count. When OCR found a different number of entries, or no
    # count line, some entries were misread or missed. WHY retry: save() reports the dialog the
    # moment its window maps, often before Qt has painted the text, and an early capture reads
    # as empty (TA2-1, the 1-in-20 flake). Each try captures again into the same file.
    for attempt in range(1, _CHECK_TRIES + 1):
        if attempt > 1:
            time.sleep(_CHECK_RETRY_SEC)
        xinput._x(sess, "import", "-window", did, "-resize", "200%", str(shot))
        text = ocr(str(shot))
        parsed = parse_checker(text)
        matches = parsed["count"] is not None and parsed["count"] == len(parsed["entries"])
        if matches:
            break
    _close_dialog(sess, did)
    _focus(sess)
    return {"clean": False, **parsed, "count_matches_entries": matches, "attempts": attempt,
            "screenshot": str(shot), "raw_text": text, "note": CHECK_NOTE}


def _cfg_open(p: argparse.ArgumentParser) -> None:
    """Add the `fred_open` verb's arguments."""
    p.add_argument("mission", help="name in data/missions (also found inside .vp) or a path")
    p.add_argument("--mods", default="")
    p.add_argument("--binary", help="qtFRED binary (default $QTFRED_BIN)")


@register("fred_open", "open a copy of a mission in qtFRED", _cfg_open)
def _cli_open(args: argparse.Namespace) -> dict:
    """CLI wrapper for open()."""
    return open(args.mission, [m for m in args.mods.split(",") if m], args.binary)


def _cfg_id(p: argparse.ArgumentParser) -> None:
    """Add the session id positional."""
    p.add_argument("id")


@register("fred_save", "save the mission copy (Ctrl+S)", _cfg_id)
def _cli_save(args: argparse.Namespace) -> dict:
    """CLI wrapper for save()."""
    return save(args.id)


@register("fred_camera", "save and read qtFRED's camera position/orientation", _cfg_id)
def _cli_camera(args: argparse.Namespace) -> dict:
    """CLI wrapper for camera()."""
    return camera(args.id)


@register("fred_selection", "read qtFRED's selection status line", _cfg_id)
def _cli_selection(args: argparse.Namespace) -> dict:
    """CLI wrapper for selection()."""
    return selection(args.id)


def _cfg_select(p: argparse.ArgumentParser) -> None:
    """Add the `fred_select` verb's arguments."""
    p.add_argument("id")
    for name in ("x1", "y1", "x2", "y2"):
        p.add_argument(name, type=int)


@register("fred_select", "marquee-select a window rectangle in qtFRED", _cfg_select)
def _cli_select(args: argparse.Namespace) -> dict:
    """CLI wrapper for select()."""
    return select(args.id, (args.x1, args.y1, args.x2, args.y2))


@register("fred_check", "run qtFRED's pre-save error check and read it (OCR)", _cfg_id)
def _cli_check(args: argparse.Namespace) -> dict:
    """CLI wrapper for check()."""
    return check(args.id)
