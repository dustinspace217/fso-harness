"""Screenshots and keyboard/mouse input on a session's virtual display.

WHY XTEST (plain `xdotool key` / `click`) and never `xdotool key --window`: SDL ignores synthetic
XSendEvent input; XTEST events look like real hardware. Coordinates are relative to the session's
main window: the pointer is placed with `mousemove --window <id>` (a pointer warp, not a synthetic
event), because windows are not at 0,0 (qtFRED's editor appeared at 512,258 with no window manager,
2026-10-12). Prefer bridge commands over keys where one exists (an earlier lab test showed F-keys
sent via XTEST never reached SDL).

The main window is found by process id (xwin.main_window): for qtFRED that skips the splash screen
and dialogs and picks the window titled with the mission file name.
"""
import argparse
import contextlib
import os
import re
import subprocess
import time
from pathlib import Path

from . import launcher, record, session, xwin
from .cli import register
from .errors import EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, HarnessError

_CROP = re.compile(r"^(\d+)x(\d+)\+(\d+)\+(\d+)$")
_KEY = re.compile(r"^[A-Za-z0-9_+]+$")
_TIMEOUT = 15
_SCROLL_MAX = 50  # wheel clicks per call; bounds the loop
_SHOT_NAME_TRIES = 1000
HOLD_MS_MAX = 10_000   # one key hold; longer would outlast the MCP call that asked for it
# How long `click` holds the button. WHY hold at all: FSO's UI samples the button once per frame
# (engine code/ui/uimouse.cpp:43-58), so a press and release inside one frame read as
# RELEASED-after-RELEASED and the button never fires (three `xdotool click`s on the debriefing's
# Statistics tab did nothing; a held press worked, 2026-10-12). 0.15 s spans two frames at the
# ~16 fps seen under Xvfb load. qtFRED does not mind: Qt only starts a drag when the pointer moves.
CLICK_HOLD_SEC = 0.15
# A double click needs both presses inside FSO's own 250 ms window, measured at frame sampling
# (ui/uimouse.cpp:68; the pilot list, list boxes and debriefing use it), and each hold and gap must
# span a frame or the release folds into the next press (the up count wins, :57-58). At the
# ~16 fps seen under Xvfb a frame is ~62 ms: 0.08 s each gives press-to-press of 2-3 frames
# (125-190 ms), inside FSO's 250 ms. QA CR-2.
_DOUBLE_HOLD_SEC = 0.08
_DOUBLE_GAP_SEC = 0.08


def parse_crop(text: str) -> tuple[int, int, int, int]:
    """Parse "WxH+X+Y" into (w, h, x, y); reject anything else with EXIT_USAGE."""
    m = _CROP.match(text or "")
    if not m or int(m.group(1)) == 0 or int(m.group(2)) == 0:
        raise HarnessError(EXIT_USAGE, f"bad crop {text!r}", "use WxH+X+Y, e.g. 400x300+10+20")
    w, h, x, y = (int(g) for g in m.groups())
    return w, h, x, y


def check_scale(value: float) -> float:
    """Return a scale factor in [0.05, 1]; reject others with EXIT_USAGE."""
    if not 0.05 <= value <= 1.0:
        raise HarnessError(EXIT_USAGE, f"bad scale {value}", "use a value between 0.05 and 1.0")
    return value


def check_keys(keys: list[str]) -> list[str]:
    """Return key names after checking they are plain xdotool key names (no shell syntax)."""
    for k in keys:
        if not _KEY.match(k):
            raise HarnessError(EXIT_USAGE, f"bad key name {k!r}",
                               "use xdotool names like Return, alt+r, KP_4")
    return keys


def _x(sess: session.Session, *args: str) -> str:
    """Run an X tool against the session display (never the user's) and return its stdout."""
    env = {**os.environ, "DISPLAY": sess.display or ""}
    env.pop("WAYLAND_DISPLAY", None)
    r = subprocess.run(list(args), env=env, capture_output=True, text=True, timeout=_TIMEOUT,
                       check=False)
    if r.returncode != 0:
        raise HarnessError(EXIT_NOT_FOUND, f"{args[0]} failed: {r.stderr.strip()[:200]}",
                           "is the session still running? `fsoctl status <id>`")
    return r.stdout


def _release(sess: session.Session, *args: str) -> None:
    """Best-effort key/button release on an error path (keyup, mouseup).

    Called only while another exception is already on its way out, so a failing release must not
    replace it: that first error is the one the caller needs. Errors here are dropped on purpose.
    """
    # The display may be gone; then nothing more can be released.
    with contextlib.suppress(HarnessError, subprocess.SubprocessError, OSError):
        _x(sess, *args)


def window_id(sess: session.Session) -> str:
    """Return the X window id of the session's game window or qtFRED editor window."""
    wid = xwin.main_window(sess.display or "", sess.engine_pid or 0, sess.kind, sess.mission)
    if not wid:
        raise HarnessError(EXIT_NOT_FOUND, "no main window for the session's process",
                           "the engine may still be starting; check `fsoctl status <id>`")
    return wid


def _move(sess: session.Session, x: int, y: int) -> None:
    """Place the pointer at (x, y) relative to the session's main window."""
    # "--" because xdotool reads a negative coordinate as an option ("unrecognized option
    # '-40'", probed 2026-10-12, QA CR-4); points above or left of the window are negative.
    _x(sess, "xdotool", "mousemove", "--window", window_id(sess), "--", str(x), str(y))


def new_shot_path(shots: Path, prefix: str) -> Path:
    """Claim and return the next free <shots>/<prefix>-NNNN.png (the file is created empty).

    WHY claim with O_EXCL: two callers that both counted the existing files and picked the same
    number would overwrite each other's image (QA CR-7). Creating the file exclusively means only
    one caller gets each name; the other moves on to the next number. Numbers stay in order, so
    a folder listing still reads as a timeline.
    """
    start = len(list(shots.glob(f"{prefix}-*.png"))) + 1
    for n in range(start, start + _SHOT_NAME_TRIES):   # bounded; collisions are rare
        path = shots / f"{prefix}-{n:04d}.png"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        return path
    raise HarnessError(EXIT_REFUSED, f"no free {prefix} file name in {shots}",
                       "clean up old screenshots with `fsoctl cleanup`")


def window_origin(sess: session.Session) -> list[int]:
    """Return [x, y] of the session's main window on its display (for mapping a full shot)."""
    out = _x(sess, "xdotool", "getwindowgeometry", "--shell", window_id(sess))
    pos = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    try:
        return [int(pos["X"]), int(pos["Y"])]
    except (KeyError, ValueError) as e:
        raise HarnessError(EXIT_NOT_FOUND, f"could not read the window position: {out[:200]!r}",
                           "is the session still running? `fsoctl status <id>`") from e


def screenshot(sid: str, crop: str | None = None, scale: float | None = None,
               full: bool = False) -> dict:
    """Capture the session window to <run>/shots/shot-NNNN.png; return path, width and height.

    full=True captures the whole virtual display instead and adds window_origin ([x, y] of the
    main window, in display coordinates). Click coordinates stay relative to the main window. To
    click a point seen in a full shot: divide by `scale` if one was given, add the crop's X/Y if
    one was given (with full=True, crop is in display coordinates), then subtract window_origin
    (QA XFA-2/XFK-5).
    WHY a separate flag: `import -window <id>` reads only that window's pixels, so Qt popup menus
    (separate override-redirect windows) come out black and qtFRED's dialogs are missing. The
    default stays the main window so existing crops and the game's own shots keep their size.
    """
    sess = launcher.require_running(sid)
    shots = session.run_dir(sess) / "shots"
    shots.mkdir(exist_ok=True)
    origin = window_origin(sess) if full else None
    args = ["import", "-window", "root" if full else window_id(sess)]
    if crop:
        w, h, x, y = parse_crop(crop)
        args += ["-crop", f"{w}x{h}+{x}+{y}", "+repage"]
    if scale:
        args += ["-resize", f"{check_scale(scale) * 100:.0f}%"]
    path = new_shot_path(shots, "shot")   # claimed last, so a bad argument leaves no empty file
    try:
        _x(sess, *args, str(path))
    except HarnessError:
        path.unlink(missing_ok=True)   # drop the empty placeholder new_shot_path created
        raise
    size = _x(sess, "identify", "-format", "%w %h", str(path)).split()
    out = {"path": str(path), "width": int(size[0]), "height": int(size[1])}
    if origin is not None:
        out["window_origin"] = origin
    return out


def _focus(sess: session.Session) -> None:
    """Give the session window keyboard focus (works without a window manager)."""
    _x(sess, "xdotool", "windowfocus", window_id(sess))


def keys(sid: str, names: list[str], hold_ms: int = 0) -> dict:
    """Press keys via XTEST, each optionally held for hold_ms milliseconds."""
    if not 0 <= hold_ms <= HOLD_MS_MAX:
        raise HarnessError(EXIT_USAGE, f"bad hold_ms {hold_ms}",
                           f"use 0 (a tap) to {HOLD_MS_MAX} milliseconds")
    sess = launcher.require_running(sid)
    check_keys(names)
    _focus(sess)
    for k in names:
        if hold_ms:
            _x(sess, "xdotool", "keydown", k)
            try:   # a key left down would keep acting in the session (cross-family X-6/XK-5)
                time.sleep(hold_ms / 1000)
            except BaseException:
                _release(sess, "xdotool", "keyup", k)
                raise
            _x(sess, "xdotool", "keyup", k)
        else:
            _x(sess, "xdotool", "key", k)
    record.append(sess, "keys", {"keys": names, "hold_ms": hold_ms})
    return {"pressed": names}


def _presses(sess: session.Session, button: str, count: int, hold: float, gap: float) -> None:
    """Press and release `button` `count` times, each held `hold` s, `gap` s apart.

    One xdotool process runs the whole sequence (its own `sleep` steps), so the timing is not
    stretched by starting a process per step, which matters for FSO's 250 ms double-click
    window. If the call is interrupted the button is released: a button left down would turn
    every later click into a drag (cross-family XK-5, same pattern as drag()).
    """
    steps: list[str] = []
    for i in range(count):            # count is 1 or 2
        if i:
            steps += ["sleep", f"{gap:g}"]
        steps += ["mousedown", button, "sleep", f"{hold:g}", "mouseup", button]
    try:
        _x(sess, "xdotool", *steps)
    except BaseException:
        _release(sess, "xdotool", "mouseup", button)
        raise


def click(sid: str, x: int, y: int, button: int = 1, double: bool = False) -> dict:
    """Move to (x, y) and click with a held press; coordinates are window coordinates.

    double=True makes two shorter held presses sized for FSO's 250 ms double-click window.
    """
    sess = launcher.require_running(sid)
    _move(sess, x, y)
    if double:
        _presses(sess, str(button), 2, _DOUBLE_HOLD_SEC, _DOUBLE_GAP_SEC)
    else:
        _presses(sess, str(button), 1, CLICK_HOLD_SEC, 0)
    record.append(sess, "click", {"x": x, "y": y, "button": button, "double": double})
    return {"clicked": [x, y]}


def drag(sid: str, start: tuple[int, int], end: tuple[int, int], button: int = 1) -> dict:
    """Press at start, move through the midpoint to end, release (marquee/orbit drags).

    start and end are (x, y) window coordinates.
    """
    sess = launcher.require_running(sid)
    (x1, y1), (x2, y2) = start, end
    mid = ((x1 + x2) // 2, (y1 + y2) // 2)
    _move(sess, x1, y1)
    _x(sess, "xdotool", "mousedown", str(button))
    try:   # a button left down turns every later click into a drag (cross-family XK-5)
        for px, py in (mid, (x2, y2)):
            time.sleep(0.3)
            _move(sess, px, py)
        time.sleep(0.3)
    except BaseException:
        _release(sess, "xdotool", "mouseup", str(button))
        raise
    _x(sess, "xdotool", "mouseup", str(button))
    record.append(sess, "drag", {"from": [x1, y1], "to": [x2, y2], "button": button})
    return {"dragged": [[x1, y1], [x2, y2]]}


def move(sid: str, x: int, y: int) -> dict:
    """Move the pointer to (x, y) in window coordinates without pressing anything.

    Returns the requested point and "pointer": where the pointer really is, in display
    coordinates (window_origin + (x, y) when the window is on screen), read back with
    `xdotool getmouselocation`. WHY a separate verb: hovering (tooltips, highlighted buttons,
    UI that reacts to a resting pointer) had no tool; click and drag always press.
    """
    sess = launcher.require_running(sid)
    _move(sess, x, y)
    out = _x(sess, "xdotool", "getmouselocation", "--shell")
    pos = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    try:
        pointer = [int(pos["X"]), int(pos["Y"])]
    except (KeyError, ValueError) as e:
        raise HarnessError(EXIT_NOT_FOUND, f"could not read the pointer position: {out[:200]!r}",
                           "is the session still running? `fsoctl status <id>`") from e
    record.append(sess, "move", {"x": x, "y": y})
    return {"moved": [x, y], "pointer": pointer}


def focus_neutral(sess: session.Session, x: int, y: int) -> None:
    """Click a neutral spot (window coordinates) so a closed modal no longer swallows input."""
    _move(sess, x, y)
    _x(sess, "xdotool", "click", "1")


def scroll(sid: str, x: int, y: int, clicks: int) -> dict:
    """Scroll the wheel at (x, y): positive clicks = up (button 4), negative = down (button 5)."""
    if abs(clicks) > _SCROLL_MAX:   # refused, not clamped: the result must match what was sent
        raise HarnessError(EXIT_USAGE, f"scroll of {clicks} clicks is over the {_SCROLL_MAX} cap",
                           f"split it into calls of at most {_SCROLL_MAX}")
    sess = launcher.require_running(sid)
    button = "4" if clicks > 0 else "5"
    _move(sess, x, y)
    for _ in range(abs(clicks)):
        _x(sess, "xdotool", "click", button)
    record.append(sess, "scroll", {"x": x, "y": y, "clicks": clicks})
    return {"scrolled": clicks}


# ---- command line ----------------------------------------------------------------------------

def _cfg_shot(p: argparse.ArgumentParser) -> None:
    """Add the `shot` verb's arguments."""
    p.add_argument("id")
    p.add_argument("--crop", help="WxH+X+Y")
    p.add_argument("--scale", type=float, help="0.05-1.0")
    p.add_argument("--full", action="store_true",
                   help="whole display (menus, dialogs); adds window_origin")


@register("screenshot", "save a PNG of the session window", _cfg_shot)
def _cli_screenshot(args: argparse.Namespace) -> dict:
    """CLI wrapper for screenshot()."""
    return screenshot(args.id, args.crop, args.scale, args.full)


def _cfg_keys(p: argparse.ArgumentParser) -> None:
    """Add the `keys` verb's arguments."""
    p.add_argument("id")
    p.add_argument("keys", nargs="+", help="xdotool key names, e.g. Return alt+r KP_4")
    p.add_argument("--hold-ms", type=int, default=0, help=f"0-{HOLD_MS_MAX}")


@register("keys", "press keys on the session window (XTEST)", _cfg_keys)
def _cli_keys(args: argparse.Namespace) -> dict:
    """CLI wrapper for keys()."""
    return keys(args.id, args.keys, args.hold_ms)


def _cfg_click(p: argparse.ArgumentParser) -> None:
    """Add the `click` verb's arguments."""
    p.add_argument("id")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--button", type=int, default=1)
    p.add_argument("--double", action="store_true")


@register("click", "click at window coordinates", _cfg_click)
def _cli_click(args: argparse.Namespace) -> dict:
    """CLI wrapper for click()."""
    return click(args.id, args.x, args.y, args.button, args.double)


def _cfg_move(p: argparse.ArgumentParser) -> None:
    """Add the `move` verb's arguments."""
    p.add_argument("id")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)


@register("move", "move the pointer to window coordinates (no click)", _cfg_move)
def _cli_move(args: argparse.Namespace) -> dict:
    """CLI wrapper for move()."""
    return move(args.id, args.x, args.y)


def _cfg_drag(p: argparse.ArgumentParser) -> None:
    """Add the `drag` verb's arguments."""
    p.add_argument("id")
    for name in ("x1", "y1", "x2", "y2"):
        p.add_argument(name, type=int)
    p.add_argument("--button", type=int, default=1)


@register("drag", "press, move and release the mouse", _cfg_drag)
def _cli_drag(args: argparse.Namespace) -> dict:
    """CLI wrapper for drag()."""
    return drag(args.id, (args.x1, args.y1), (args.x2, args.y2), args.button)


def _cfg_scroll(p: argparse.ArgumentParser) -> None:
    """Add the `scroll` verb's arguments."""
    p.add_argument("id")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("clicks", type=int, help="positive = up, negative = down")


@register("scroll", "turn the mouse wheel at a point", _cfg_scroll)
def _cli_scroll(args: argparse.Namespace) -> dict:
    """CLI wrapper for scroll()."""
    return scroll(args.id, args.x, args.y, args.clicks)
